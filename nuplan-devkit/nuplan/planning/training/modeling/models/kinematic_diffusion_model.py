from typing import cast

import torch
import torch.nn as nn
import numpy as np

from diffusers import DDIMScheduler

from nuplan.planning.training.modeling.types import FeaturesType, TargetsType
from nuplan.planning.training.preprocessing.features.trajectory import Trajectory
from nuplan.planning.training.modeling.models.urban_driver_open_loop_model import (
    convert_predictions_to_trajectory,
)
from nuplan.planning.training.modeling.models.diffusion_utils import (
    Standardizer,
    SinusoidalPosEmb,
    VerletStandardizer
)
from nuplan.planning.training.modeling.torch_module_wrapper import TorchModuleWrapper
from nuplan.planning.training.modeling.models.encoder_decoder_layers import (
    ParallelAttentionLayer
)
from nuplan.planning.training.preprocessing.features.agent_history import AgentHistory
from nuplan.planning.training.preprocessing.feature_builders.agent_history_feature_builder import (
    AgentHistoryFeatureBuilder,
)
from nuplan.planning.training.preprocessing.feature_builders.vector_set_map_feature_builder import (
    VectorSetMapFeatureBuilder,
)
from nuplan.planning.training.preprocessing.target_builders.ego_trajectory_target_builder import (
    EgoTrajectoryTargetBuilder,
)
from nuplan.planning.training.preprocessing.target_builders.agent_trajectory_target_builder import (
    AgentTrajectoryTargetBuilder
)
from nuplan.planning.training.modeling.models.positional_embeddings import RotaryPositionEncoding


class KinematicDiffusionModel(TorchModuleWrapper):
    def __init__(
        self,
        feature_dim,
        past_trajectory_sampling,
        future_trajectory_sampling,
        map_params,
        T: int = 32,
        predictions_per_sample: int = 16, # 一个样本预测的轨迹数量
        max_dist: float = 200,           # used to normalize ALL tokens (incl. trajectory)
        easy_validation: bool = False,  # instead of starting from pure noise, start with not that much noise at inference,
        use_verlet: bool = True,
        ignore_history: bool = False
    ):
        super().__init__(
            feature_builders=[
                AgentHistoryFeatureBuilder(
                    trajectory_sampling=past_trajectory_sampling,
                    max_agents=10
                ),
                VectorSetMapFeatureBuilder(
                    map_features=map_params['map_features'],
                    max_elements=map_params['max_elements'],
                    max_points=map_params['max_points'],
                    radius=map_params['vector_set_map_feature_radius'],
                    interpolation_method=map_params['interpolation_method']
                )
            ],
            target_builders=[
                EgoTrajectoryTargetBuilder(future_trajectory_sampling),
                AgentTrajectoryTargetBuilder(
                    trajectory_sampling=past_trajectory_sampling,
                    future_trajectory_sampling=future_trajectory_sampling,
                    max_agents=10
                )
            ],
            future_trajectory_sampling=future_trajectory_sampling
        )

        self.feature_dim = feature_dim
        self.T = T
        self.H = 16 # 轨迹时间维度大小
        self.output_dim = self.H * 3 # 轨迹时间维度大小 x (x,y,theta) 即一条轨迹的维度大小
        self.predictions_per_sample = predictions_per_sample # 一个样本预测的轨迹数量
        self.max_dist = max_dist
        self.easy_validation = easy_validation
        self.use_verlet = use_verlet
        self.ignore_history = ignore_history

        self.standardizer = VerletStandardizer() if use_verlet else Standardizer(max_dist=max_dist)

        # DIFFUSER
        self.scheduler = DDIMScheduler(
            num_train_timesteps=self.T,
            beta_schedule='scaled_linear',
            prediction_type='epsilon',
        )
        self.scheduler.set_timesteps(self.T)

        self.history_encoder = nn.Sequential(
            nn.Linear(7, self.feature_dim),
            nn.ReLU(),
            nn.Linear(self.feature_dim, self.feature_dim)
        )

        self.sigma_encoder = nn.Sequential(
            SinusoidalPosEmb(self.feature_dim),
            nn.Linear(self.feature_dim, self.feature_dim),
            nn.ReLU(),
            nn.Linear(self.feature_dim, self.feature_dim)
        )
        self.sigma_proj_layer = nn.Linear(self.feature_dim * 2, self.feature_dim)

        self.trajectory_encoder = nn.Linear(3, self.feature_dim)
        self.trajectory_time_embeddings = RotaryPositionEncoding(self.feature_dim)

        # 类别embedding：模型可以根据输入类别的不同，自动调整这些嵌入向量，从而提升模型的表现
        # 0: 场景
        # 1: 轨迹
        # 2: 噪声
        self.type_embedding = nn.Embedding(3, self.feature_dim) # trajectory, noise token

        self.global_attention_layers = torch.nn.ModuleList([
            ParallelAttentionLayer(
                d_model=self.feature_dim, 
                self_attention1=True, self_attention2=False,
                cross_attention1=False, cross_attention2=False,
                rotary_pe=True
            )
        for _ in range(8)])
        
        # 解码器，多层感知机，特征到三维轨迹
        self.decoder_mlp = nn.Sequential(
            nn.Linear(self.feature_dim, self.feature_dim),
            nn.ReLU(),
            nn.Linear(self.feature_dim, 3)
        )

        self.all_trajs = []
        self.apply(self._init_weights)
        self.precompute_variances()

    # 初始化module的权重
    def _init_weights(self, module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    # 预先计算方差
    def precompute_variances(self):
        """
        Precompute variances from alphas
        """
        sqrt_alpha_prod = self.scheduler.alphas_cumprod[self.scheduler.timesteps] ** 0.5
        sqrt_one_minus_alpha_prod = (1 - self.scheduler.alphas_cumprod[self.scheduler.timesteps]) ** 0.5
        self._variances = sqrt_one_minus_alpha_prod / sqrt_alpha_prod

    
    def sigma(self, t):
        return t

    def forward(self, features: FeaturesType, num_grad_steps=15, step_size=.001, use_clean=False) -> TargetsType:
        # Recover features
        ego_agent_features = cast(AgentHistory, features["agent_history"]) # 包括自车历史、其他agent历史、mask标记有效的时间步
        batch_size = ego_agent_features.batch_size

        state_features = self.encode_scene_features(ego_agent_features) # ego的历史轨迹特征，场景type embedding

        # Only use for denoising
        if 'trajectory' in features:
            ego_gt_trajectory = features['trajectory'].data.clone() # B x H x D
            # 归一化 x,y,theta到[0,1]
            ego_gt_trajectory = self.standardizer.transform_features(ego_agent_features, ego_gt_trajectory)

        if self.training:
            noise = torch.randn(ego_gt_trajectory.shape, device=ego_gt_trajectory.device)
            timesteps = torch.randint(0, self.scheduler.config.num_train_timesteps, (ego_gt_trajectory.shape[0],), 
                                        device=ego_gt_trajectory.device).long() # 扩散的时间步数

            ego_noisy_trajectory = self.scheduler.add_noise(ego_gt_trajectory, noise, timesteps)

            pred_noise = self.denoise(ego_noisy_trajectory, timesteps, state_features)

            output = {
                # TODO: fix trajectory, right now we only care about epsilon at training and this is a dummy value
                "trajectory": Trajectory(data=convert_predictions_to_trajectory(pred_noise)), # 这个是nuplan训练一定要有的
                "epsilon": pred_noise,
                "gt_epsilon": noise
            }
            return output
        else: # 实际上没有用到，不用看，而且这一部分有点问题 作者说这是梯度引导的扩散，下面的batch都为1
            # Multiple predictions per sample
            # repeat_interleave 在指定维度上重复插入
            state_features = (
                state_features[0].repeat_interleave(self.predictions_per_sample,0),
                state_features[1].repeat_interleave(self.predictions_per_sample,0)
            )
            ego_agent_features = ego_agent_features.repeat_interleave(self.predictions_per_sample,0)

            # Sampling / inference
            # squeeze(0) 如果第一个维度为1,就去掉第一个维度（批次维度）。
            if 'warm_start' in features:
                ego_trajectory = features['warm_start'].clone().to(state_features[0].device).squeeze(0)
                ego_trajectory = self.standardizer.transform_features(ego_agent_features, ego_trajectory)
                timesteps = self.scheduler.timesteps[features['warm_start_steps']:]
                noise = torch.randn(ego_trajectory.shape, device=ego_trajectory.device)
                ego_trajectory = self.scheduler.add_noise(ego_trajectory, noise, timesteps[0])
            else:
                ego_trajectory = torch.randn((batch_size * self.predictions_per_sample, self.H * 3), device=state_features[0].device) # 轨迹数量 x 轨迹维度
                timesteps = self.scheduler.timesteps

            for t in timesteps:
                residual = torch.zeros_like(ego_trajectory)

                # If constraints exist, then compute epsilons and add them
                # 有点像条件扩散啊
                if 'constraints' in features:
                    with torch.enable_grad():
                        for _ in range(num_grad_steps):
                            ego_trajectory.requires_grad_(True)
                            ego_trajectory_unstd = self.standardizer.untransform_features(ego_agent_features, ego_trajectory)
                            constraint_scores, _ = compute_constraint_scores(features['constraints'], ego_trajectory_unstd)
                            # torch.autograd.grad 返回一个元组，其中第一个元素是所需的梯度
                            grad = torch.autograd.grad([constraint_scores.mean()], [ego_trajectory])[0]
                            posterior_variance = self.scheduler._get_variance(t,t-1)
                            model_std = torch.exp(0.5 * posterior_variance)
                            # 使用模型的标准差调整计算得到的梯度 标准差越大，梯度的调整幅度也会相应增大，从而更好地探索状态空间。
                            grad = model_std * grad
                            ego_trajectory = ego_trajectory.detach()
                            ego_trajectory = ego_trajectory - step_size * grad # traj向着constraint_scores减小的方向前进

                with torch.no_grad():
                    residual += self.denoise(ego_trajectory, t.to(ego_trajectory.device), state_features)

                out = self.scheduler.step(residual, t, ego_trajectory)
                ego_trajectory = out.prev_sample

            ego_trajectory = self.standardizer.untransform_features(ego_agent_features, ego_trajectory) # (B x predictions_per_sample,H x 3)

            if 'constraints' in features:
                # If we have constraints, we can take the trajectory with the lowest constraint cost
                scores, _ = compute_constraint_scores(features['constraints'], ego_trajectory)
                ego_trajectory = ego_trajectory.reshape(batch_size, self.predictions_per_sample, self.output_dim)
                scores = scores.reshape(batch_size, self.predictions_per_sample)
                best_trajectory = ego_trajectory[range(batch_size), scores.argmin(dim=1)]
                
                # 约束残差 表示约束违反的程度，输出的grad是constraints对trajectory的梯度，可看作是减少约束残差的方向
                constraint_residual = compute_constraint_residual(features['constraints'], ego_trajectory.reshape(-1,self.output_dim)) # (B x predictions_per_sample,H x 3)
                # 每个样本最小分数对应的残差值
                print("!!!! I'm here") # TODO 下面这句话好像有点问题，实际上没有用到吧
                constraint_residual = constraint_residual[None][range(batch_size), scores.argmin(dim=1)]
            else:
                # TODO: this is arbitrary 没有约束方程 随便选一个
                ego_trajectory = ego_trajectory.reshape(batch_size, self.predictions_per_sample, self.output_dim)
                best_trajectory = ego_trajectory[:,0]

            return {
                "trajectory": Trajectory(data=convert_predictions_to_trajectory(best_trajectory)),
                "multimodal_trajectories": ego_trajectory,
                "grad": convert_predictions_to_trajectory(constraint_residual) if 'constraints' in features else None,
                # "scores": scores[0]
            }

    def encode_scene_features(self, ego_agent_features):
        # TODO 理论上有优化空间，场景特征应该不止ego的历史特征：七个维度的量
        ego_features = ego_agent_features.ego
        if self.ignore_history:
            ego_features = torch.zeros_like(ego_features)
        ego_features = self.history_encoder(ego_features) # Bx5x7 -> Bx5xD (batch size x 时间步 x 特征维度)

        ego_type_embedding = self.type_embedding(torch.as_tensor([[0]], device=ego_features.device))
        ego_type_embedding = ego_type_embedding.repeat(ego_features.shape[0],5,1)

        return ego_features, ego_type_embedding

    def denoise(self, ego_trajectory, sigma, state_features):
        batch_size = ego_trajectory.shape[0]

        state_features, state_type_embedding = state_features
        
        # Trajectory features
        ego_trajectory = ego_trajectory.reshape(ego_trajectory.shape[0],self.H,3)
        trajectory_features = self.trajectory_encoder(ego_trajectory)

        trajectory_type_embedding = self.type_embedding(
            torch.as_tensor([1], device=ego_trajectory.device)
        )[None].repeat(batch_size,self.H,1)

        # Concatenate all features
        all_features = torch.cat([state_features, trajectory_features], dim=1)
        all_type_embedding = torch.cat([state_type_embedding, trajectory_type_embedding], dim=1)

        # Sigma encoding
        # .reshape(-1, 1) 会将张量调整为具有两维的张量，其中第一个维度的大小自动计算（压扁为一列），第二个维度固定为 1。
        sigma = sigma.reshape(-1,1)
        # .numel() 是用于获取张量中所有元素的总数量的操作。
        if sigma.numel() == 1:
            sigma = sigma.repeat(batch_size,1)
        sigma = sigma.float() / self.T
        sigma_embeddings = self.sigma_encoder(sigma)

        # 增加时间维度
        sigma_embeddings = sigma_embeddings.reshape(batch_size,1,self.feature_dim)

        # Concatenate sigma features and project back to original feature_dim
        # 将 sigma_embeddings 扩展，使其与 all_features 的时序长度匹配。
        sigma_embeddings = sigma_embeddings.repeat(1,all_features.shape[1],1)
        # 沿着特征维度（即 dim=2）拼接在一起
        all_features = torch.cat([all_features, sigma_embeddings], dim=2)
        # all_features 保留了原本的特征维度大小，同时结合了 sigma 的信息。
        all_features = self.sigma_proj_layer(all_features)

        # Generate attention mask
        seq_len = all_features.shape[1]
        indices = torch.arange(seq_len, device=all_features.device)
        # indices[None] 前面加一个维度 seq_len, 变成 1,seq_len
        # indices[:,None] 后面加一个维度 seq_len, 变成 seq_len,1
        dists = (indices[None] - indices[:,None]).abs()
        attn_mask = dists > 1       # TODO: magic number

        # Generate relative temporal embeddings
        temporal_embedding = self.trajectory_time_embeddings(indices[None].repeat(batch_size,1))

        # Global self-attentions
        for layer in self.global_attention_layers:            
            all_features, _ = layer(
                all_features, None, None, None,
                seq1_pos=temporal_embedding,
                seq1_sem_pos=all_type_embedding,
                attn_mask_11=attn_mask
            )

        trajectory_features = all_features[:,-self.H:]
        out = self.decoder_mlp(trajectory_features).reshape(trajectory_features.shape[0],-1)

        return out # , all_weights

    def run_diffusion_es(
        self, 
        features: FeaturesType, 
        warm_start = None, 
        use_cem=False,
        cem_iters=1, # 搜索20步
        num_elites=32,
        temperature=0.1,
    ) -> TargetsType:
        # Recover features
        ego_agent_features = cast(AgentHistory, features["agent_history"])
        # TODO 为什么ego_agent_features会有batch_size 因为训练都是批量进行的？
        batch_size = ego_agent_features.batch_size * self.predictions_per_sample

        state_features = self.encode_scene_features(ego_agent_features) # 自车特征 和 场景 embedding

        # Only use for denoising 似乎没啥用
        if 'trajectory' in features:
            ego_gt_trajectory = features['trajectory'].data.clone()
            ego_gt_trajectory = self.standardizer.transform_features(ego_agent_features, ego_gt_trajectory)

        # Multiple predictions per sample
        state_features = (
            state_features[0].repeat_interleave(self.predictions_per_sample,0),
            state_features[1].repeat_interleave(self.predictions_per_sample,0)
        )
        ego_agent_features = ego_agent_features.repeat_interleave(self.predictions_per_sample,0)

        trunc_step_schedule = np.linspace(5,1,cem_iters).astype(int)
        noise_scale = 1.0

        trajectory_shape = (batch_size, self.H * 3)
        device = state_features[0].device

        use_warm_start = warm_start is not None

        # Initialize elite set with random 
        noise = torch.randn(trajectory_shape, device=device)
        population_trajectories, population_scores, population_info = self.rollout(
            features,
            state_features,
            noise,
            initial_rollout=True,
            deterministic=False,
        )

        # If warm start, add those to initial set
        if use_warm_start:
            prev_trajectories = warm_start
            prev_trajectories = prev_trajectories.to(device)
            # Recompute scores
            prev_scores, prev_info = compute_constraint_scores(features['constraints'], prev_trajectories)
            num_warm_samples = prev_trajectories.shape[0]
            # Concatenate to initial elite set
            # 将初始种群的最后几个替换成warm_start的个体
            population_trajectories = torch.cat([population_trajectories[:-num_warm_samples], prev_trajectories], dim=0)
            population_scores = torch.cat([population_scores[:-num_warm_samples], prev_scores], dim=0)
            if 'traj_sim' in prev_info:
                population_info['traj_sim'][-num_warm_samples:] = prev_info['traj_sim']

        for i in range(cem_iters):
            population_trajectories = self.standardizer.transform_features(ego_agent_features, population_trajectories)
            n_trunc_steps = trunc_step_schedule[i]

            """
            Local MPPI update
            """
            # Compute reward-probabilities
            reward_probs = torch.exp(temperature * -population_scores)
            reward_probs = reward_probs / reward_probs.sum()
            probs = reward_probs

            """
            Resample and mutate (renoise-denoise)
            """
            if use_cem:
                elites = torch.argsort(population_scores)[:num_elites]
                indices = torch.randint(0, num_elites, (batch_size,), device=device)
                population_trajectories = population_trajectories[elites[indices]]
                population_trajectories = self.renoise(population_trajectories, n_trunc_steps)
            else:
                # 按照上一代种群分数构建的概率分布采样下一代轨迹
                indices = torch.multinomial(probs, batch_size, replacement=True) # torch.multinomial(probs, 1).squeeze(1)
                population_trajectories = population_trajectories[indices]
                population_trajectories = self.renoise(population_trajectories, n_trunc_steps)

            # Denoise
            population_trajectories, population_scores, population_info = self.rollout(
                features,
                state_features,
                population_trajectories,
                initial_rollout=False,
                deterministic=False,
                n_trunc_steps=n_trunc_steps,
                noise_scale=noise_scale,
            )


        best_trajectory = population_trajectories[population_scores.argmin()]
        best_trajectory = best_trajectory.reshape(-1,16,3)

        out = {
            "trajectory": Trajectory(data=convert_predictions_to_trajectory(best_trajectory)),
            "multimodal_trajectories": population_trajectories,
            "scores": population_scores,
        }
        if 'traj_sim' in population_info:
            out.update({'traj_sim': population_info['traj_sim']})

        return out
    
    def rollout(
        self,
        features,
        state_features,
        ego_trajectory,
        initial_rollout=True, 
        deterministic=True, 
        n_trunc_steps=5, 
        noise_scale=1.0, 
        ablate_diffusion=False
    ):
        if initial_rollout:
            timesteps = self.scheduler.timesteps
        else:
            timesteps = self.scheduler.timesteps[-n_trunc_steps:]

        if ablate_diffusion and not initial_rollout:
            timesteps = []

        for t in timesteps:
            residual = torch.zeros_like(ego_trajectory)

            with torch.no_grad():
                # 每一步预测的噪声
                residual += self.denoise(ego_trajectory, t.to(ego_trajectory.device), state_features)

            if deterministic:
                eta = 0.0 # 逆向过程再加的噪声的系数
            else:
                prev_alpha = self.scheduler.alphas[t-1]
                alpha = self.scheduler.alphas[t]
                eta = noise_scale * torch.sqrt((1 - prev_alpha) / (1 - alpha)) * \
                        torch.sqrt((1 - alpha) / prev_alpha)

            out = self.scheduler.step(residual, t, ego_trajectory, eta=eta)
            ego_trajectory = out.prev_sample

        ego_agent_features = cast(AgentHistory, features["agent_history"])
        ego_trajectory = self.standardizer.untransform_features(ego_agent_features, ego_trajectory)
        scores, info = compute_constraint_scores(features['constraints'], ego_trajectory)

        return ego_trajectory, scores, info

    def renoise(self, ego_trajectory, t):
        noise = torch.randn(ego_trajectory.shape, device=ego_trajectory.device)
        ego_trajectory = self.scheduler.add_noise(ego_trajectory, noise, self.scheduler.timesteps[-t])
        return ego_trajectory


def compute_constraint_scores(constraints, trajectory):
    all_info = {}
    total_cost = torch.zeros(trajectory.shape[0], device=trajectory.device)
    # constraints函数可以有多个
    for constraint in constraints:
        cost, info = constraint(trajectory) # 调用constraint_fn
        total_cost += cost
        all_info.update(info)
    return total_cost, all_info


def compute_constraint_residual(constraints, trajectory):
    """
    Compute the gradient of the sum of all the constraints w.r.t trajectory
    """
    with torch.enable_grad():
        trajectory.requires_grad_(True)
        total_cost, _ = compute_constraint_scores(constraints, trajectory)
        total_cost.mean().backward() # 反向传播
        grad = trajectory.grad
        trajectory.requires_grad_(False)
    return grad
