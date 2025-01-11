from typing import Dict, List, cast

import torch
import torch.nn.functional as F

from nuplan.planning.training.modeling.objectives.abstract_objective import AbstractObjective
from nuplan.planning.training.modeling.types import FeaturesType, ScenarioListType, TargetsType


class DiffusionPVAObjective(AbstractObjective):
    def __init__(self, scenario_type_loss_weighting: Dict[str, float], weight: float = 1.0):
        pass

    def name(self) -> str:
        """
        Name of the objective
        """
        return 'diffusion_pva_objective'

    def get_list_of_required_target_types(self) -> List[str]:
        """Implemented. See interface."""
        return []

    def compute(self, predictions: FeaturesType, targets: TargetsType, scenarios: ScenarioListType) -> torch.Tensor:
        # 重要：揭示了扩散模型怎么训练的，不断让pred和gt接近！而且targets没有用到！
        # 因为在扩散模型中，预测的不是ego_gt，而是刚开始生成的白噪声，ego_gt只是加噪的初始状态
        if 'epsilon' in predictions:
            # pos
            pred = predictions['epsilon']
            gt = predictions['gt_epsilon']
            loss_p = F.mse_loss(pred, gt)
            # # vel
            # pred_v = torch.diff(pred, dim=1)  # B x (H-1) x D
            # gt_v = torch.diff(gt, dim=1)
            # loss_v = F.mse_loss(pred_v, gt_v)
            # # acc
            # pred_a = torch.diff(pred_v, dim=1)  # B x (H-2) x D
            # gt_a = torch.diff(gt_v, dim=1)
            # loss_a = F.mse_loss(pred_a, gt_a)
            # 4. 合并损失（可以加权）
            # alpha, beta, gamma = 7.0, 2.0, 1.0  # 位置、速度、加速度的权重
            # loss = alpha * loss_p + beta * loss_v + gamma * loss_a
            loss = loss_p
            # print(loss_p,"loss_p")
            # print(loss_v,"loss_v")
            # print(loss_a,"loss_a")
        else:
            # this objective is meaningless at test time
            # 如果在测试阶段未提供 epsilon，则返回一个零值损失。这表明在测试时该目标函数没有意义
            loss = torch.as_tensor(0., device=predictions['trajectory'].data.device)
        return loss
