from typing import List, Optional, cast
import gc
import os
import time
import io
import copy

import numpy as np
from scipy.interpolate import interp1d
from omegaconf import OmegaConf
import torch
from PIL import Image
import cv2
from matplotlib import colormaps
import matplotlib.pyplot as plt
from shapely import Point
import numpy.typing as npt

from nuplan.common.actor_state.ego_state import EgoState
from nuplan.common.actor_state.state_representation import TimePoint
from nuplan.common.maps.abstract_map_objects import LaneGraphEdgeMapObject
from nuplan.common.maps.nuplan_map.lane import NuPlanLane
from nuplan.common.maps.nuplan_map.lane_connector import NuPlanLaneConnector
from nuplan.planning.simulation.planner.abstract_planner import PlannerInput
from nuplan.planning.simulation.trajectory.interpolated_trajectory import (
    InterpolatedTrajectory,
)
from nuplan.planning.simulation.trajectory.trajectory_sampling import TrajectorySampling
from nuplan.planning.simulation.planner.ml_planner.model_loader import ModelLoader
from nuplan.planning.script.builders.model_builder import build_torch_module_wrapper
from nuplan.planning.training.modeling.lightning_module_wrapper import LightningModuleWrapper
from nuplan.planning.training.preprocessing.features.trajectory import Trajectory
from nuplan.planning.simulation.planner.ml_planner.transform_utils import transform_predictions_to_states
from nuplan.planning.training.callbacks.utils.visualization_utils import visualize
from nuplan.common.maps.maps_datatypes import SemanticMapLayer
from nuplan.common.actor_state.tracked_objects_types import (
    TrackedObjectType,
)


from tuplan_garage.planning.simulation.planner.pdm_planner.scoring.pdm_scorer import (
        PDMScorer,
    )
from tuplan_garage.planning.simulation.planner.pdm_planner.abstract_pdm_planner import (
    AbstractPDMPlanner,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.observation.pdm_observation import (
    PDMObservation,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.proposal.batch_idm_policy import (
    BatchIDMPolicy,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.proposal.pdm_generator import (
    PDMGenerator,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.proposal.pdm_proposal import (
    PDMProposalManager,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.simulation.pdm_simulator import (
    PDMSimulator,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_emergency_brake import (
    PDMEmergencyBrake,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_geometry_utils import (
    parallel_discrete_path,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_path import PDMPath
from tuplan_garage.planning.simulation.planner.pdm_planner.observation.pdm_observation_utils import (
    get_drivable_area_map,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_array_representation import (
    states_se2_to_array,
    state_array_to_ego_states,
    ego_states_to_state_array
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_enums import (
    MultiMetricIndex,
    WeightedMetricIndex,
    BBCoordsIndex
)
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.lane_graph_parser import parse_lane_graph
from tuplan_garage.planning.simulation.planner.pdm_planner.utils.diffusion_utils import (
    interpolate_trajectory,
    convert_to_local,
    convert_to_global,
    fig_to_numpy
)
import time


def seed_everything(seed=123):
    torch.manual_seed(seed)
    np.random.seed(seed)


class DEPDMPlanner(AbstractPDMPlanner):
    """
    Interface for planners incorporating PDM-Closed. Used for PDM-Closed and PDM-Hybrid.
    """

    requires_scenario = True

    def __init__(
        self,
        trajectory_sampling: TrajectorySampling,
        proposal_sampling: TrajectorySampling,
        idm_policies: BatchIDMPolicy,
        lateral_offsets: Optional[List[float]],
        map_radius: float,
        checkpoint_path: str,
        dump_gifs_path: str,
        scenario,
        scorer_config,
        comfort_config,

        follow_centerline=False,
        use_idm_speed=False,

        use_pdm_proposals=False,
        use_pdm_closed_only=False,

        time_idxs_to_save=[],
    ):
        """
        Constructor for AbstractPDMClosedPlanner
        :param trajectory_sampling: Sampling parameters for final trajectory
        :param proposal_sampling: Sampling parameters for proposals
        :param idm_policies: BatchIDMPolicy class
        :param lateral_offsets: centerline offsets for proposals (optional)
        :param map_radius: radius around ego to consider
        """

        super(DEPDMPlanner, self).__init__(map_radius)

        assert (
            trajectory_sampling.interval_length == proposal_sampling.interval_length
        ), "AbstractPDMClosedPlanner: Proposals and Trajectory must have equal interval length!"

        # config parameters
        self._trajectory_sampling: int = trajectory_sampling
        self._proposal_sampling: int = proposal_sampling
        self._idm_policies: BatchIDMPolicy = idm_policies
        self._lateral_offsets: Optional[List[float]] = lateral_offsets
        
        self._checkpoint_path = checkpoint_path
        self._dump_gifs_path = dump_gifs_path

        # observation/forecasting class
        self._observation = PDMObservation(
            trajectory_sampling, trajectory_sampling if not os.environ.get('CLOSED', 0) else proposal_sampling, map_radius
        )

        # proposal/trajectory related classes
        self._generator = PDMGenerator(trajectory_sampling, trajectory_sampling if not os.environ.get('CLOSED', 0) else proposal_sampling)
        self._simulator = PDMSimulator(proposal_sampling)
        self._scorer = PDMScorer(proposal_sampling, scorer_config, comfort_config)
        self._emergency_brake = PDMEmergencyBrake(trajectory_sampling)

        # lazy loaded
        self._proposal_manager: Optional[PDMProposalManager] = None
        self._speed_limit = None

        self._replan_freq = 5 # TODO
        self._diffusion_freq_low = 10
        self._diffusion_freq = 10
        self._diffusion_freq_high = 5
        self._diffusion_count = 0


        # good results interplan
        self._diffusion_threshold = 2
        self._pdm_threshold = 1

        self._planner = "PDM"
        self._trajectory = None

        self._pdm_99_count = 0
        self._trapped_count = 0
        self._trapped = False

        # warm start
        self._use_warm_start = False
        self._prev_predictions = None

        # viz
        self._frames = []

        self._log_name = scenario.log_name
        self._token = scenario.token
        self._scenario_type = scenario.scenario_type

        self._yield_token = None
        self._lane_goal = None

        self._follow_centerline = follow_centerline
        self._use_idm_speed = use_idm_speed
        self._use_pdm_proposals = use_pdm_proposals
        self._use_pdm_closed_only = use_pdm_closed_only
        self._time_idxs_to_save = time_idxs_to_save

    def name(self) -> str:
        """Inherited, see superclass."""
        return self.__class__.__name__

    def observation_type(self):
        """Inherited, see superclass."""
        return DetectionsTracks  # type: ignore
    
    def compute_planner_trajectory(
        self, current_input: PlannerInput
    ):
        """Inherited, see superclass."""

        gc.disable()
        self.ego_state, _ = current_input.history.current_state

        # Apply route correction on first iteration (ego_state required)
        if self._iteration == 0:
            self._route_roadblock_correction(self.ego_state)

            # route_roadblock_ids = route_roadblock_correction2(
            #     self.ego_state, self._initialization.mission_goal, self._map_api, self._route_roadblock_dict
            # )
            # self._load_route_dicts(route_roadblock_ids)

        # Update/Create drivable area polygon map
        self._drivable_area_map = get_drivable_area_map(
            self._map_api, self.ego_state, self._map_radius,
            self._route_lane_dict
        )

        start_time = time.time()  # 获取开始时间
        trajectory = self._get_closed_loop_trajectory(current_input)
        end_time = time.time()  # 获取结束时间
        execution_time = end_time - start_time  # 计算运行时间
        self._execution_times.append([self._iteration, execution_time])  # 将每次的运行时间保存到列表中
        self._planner_types.append([self._iteration, self._planner=="Diffusion"])  # 将每次的运行时间保存到列表中
        self._iteration += 1
        return trajectory

    def initialize(self, initialization) -> None:
        """Inherited, see superclass."""
        
        self._initialization = initialization
        self._iteration = 0
        self._map_api = initialization.map_api
        self._load_route_dicts(initialization.route_roadblock_ids)

        gc.collect()

        # model loading
        config_path = '/'.join(self._checkpoint_path.split('/')[:-2])
        config_path = os.path.join(config_path, 'code/hydra/config.yaml')
        model_config = OmegaConf.load(config_path).model


        torch_module_wrapper = build_torch_module_wrapper(model_config)
        self._model = LightningModuleWrapper.load_from_checkpoint(
            self._checkpoint_path, model=torch_module_wrapper
        ).model

        self._model_loader = ModelLoader(self._model)
        self._model_loader.initialize()

        # TODO: magic
        self._model.predictions_per_sample = 256
        # 查看显存占用情况
        print(f"显存已分配: {torch.cuda.memory_allocated() / 1024**2:.2f} MiB")

        self._trajectory_diffusion = None
        self._trajectory_pdm = None

        self._execution_times = []
        self._planner_types = []

        seed_everything(int(os.environ.get('SEED',0))) # 不能注释，不然一起跑的话时间结果和单个跑的时候不一样

    def _update_proposal_manager(self, ego_state: EgoState):
        """
        Updates or initializes PDMProposalManager class
        :param ego_state: state of ego-vehicle
        """

        self._current_lane = self._get_starting_lane(ego_state)
        create_new_proposals = self._iteration == 0

        if create_new_proposals:
            proposal_paths: List[PDMPath] = self._get_proposal_paths(self._current_lane)

            self._proposal_manager = PDMProposalManager(
                lateral_proposals=proposal_paths,
                longitudinal_policies=self._idm_policies,
            )

        # update proposals
        self._proposal_manager.update(self._current_lane.speed_limit_mps)

        # set speed limit
        if isinstance(self._current_lane, NuPlanLane):
            self._speed_limit = self._current_lane.speed_limit_mps
        else:
            # if its a lane connector, get the speed limit from adjacent lanes
            edges = self._current_lane.incoming_edges + self._current_lane.outgoing_edges + [self._current_lane]
            speed_limits = [edge.speed_limit_mps for edge in edges if edge.speed_limit_mps is not None]
            self._speed_limit = max(speed_limits) if len(speed_limits) > 0 else 100




    def _get_proposal_paths(
        self, current_lane: LaneGraphEdgeMapObject
    ) -> List[PDMPath]:
        """
        Returns a list of path's to follow for the proposals. Inits a centerline.
        :param current_lane: current or starting lane of path-planning
        :return: lists of paths (0-index is centerline)
        """
        centerline_discrete_path = self._get_discrete_centerline(current_lane)
        self._centerline = PDMPath(centerline_discrete_path)

        # 1. save centerline path (necessary for progress metric)
        output_paths: List[PDMPath] = [self._centerline]

        # 2. add additional paths with lateral offset of centerline
        if self._lateral_offsets is not None:
            for lateral_offset in self._lateral_offsets:
                offset_discrete_path = parallel_discrete_path(
                    discrete_path=centerline_discrete_path, offset=lateral_offset
                )
                output_paths.append(PDMPath(offset_discrete_path))

        return output_paths

    def _get_closed_loop_trajectory(
        self,
        current_input: PlannerInput,
    ) -> InterpolatedTrajectory:
        """
        Creates the closed-loop trajectory for PDM-Closed planner.
        :param current_input: planner input
        :return: trajectory
        """
        # # diffusion_count法
        # if self._diffusion_count >= 1:
        #     self._diffusion_count = self._diffusion_count + 1
        # if self._diffusion_count == 17:
        #     self._diffusion_count = 0
        
        print("---------------------------------",self._iteration,"-------------------------------------")
        if self._diffusion_count == 0 and self._iteration % self._replan_freq == 0:
                


            ego_state, observation = current_input.history.current_state
            if self._planner == "PDM" or self._iteration % self._diffusion_freq == 0:
                ##################################### PDM #####################################
                # 1. Environment forecast and observation update
                self._observation.update(
                    ego_state,
                    observation,
                    current_input.traffic_light_data,
                    self._route_lane_dict,
                )
                # 一些初始化
                

                # 2. Centerline extraction and proposal update
                self._update_proposal_manager(ego_state)

                # 3. Generate/Unroll proposals
                # Generate PDM-Closed proposals
                pdm_proposals_global = self._generator.generate_proposals(
                    ego_state, self._observation, self._proposal_manager
                )
                print("前面的agent有：",self._generator._num_agent_ahead)
                print("前面的静态物有：",self._generator._num_static_object_ahead)
                
                
                # 4. Simulate proposals
                simulated_proposals_array = self._simulator.simulate_proposals(
                    pdm_proposals_global, ego_state
                )

                # lead_agent
                # Set IDM-determined speed limit
                if self._use_idm_speed:
                    self._speed_limit = self._generator.get_max_speed()
                self._lead_agent = self._generator.get_lead_agent()
                self._red_light = self._generator._red_light

                # 5. Score proposals
                state_history = ego_states_to_state_array(current_input.history.ego_states)

                proposal_scores = self._scorer.score_proposals(
                    simulated_proposals_array, # 此处确定的scorer.states
                    self.ego_state,
                    self._observation,
                    self._centerline,
                    self._route_lane_dict,
                    self._drivable_area_map,
                    self._map_api,
                    self._speed_limit,
                    self._lead_agent,
                    state_history,
                )

                # 6.a Apply brake if emergency is expected
                trajectory = self._emergency_brake.brake_if_emergency(
                    ego_state, proposal_scores, self._scorer
                )

                current_time_point = copy.deepcopy(self.ego_state.time_point)
                time_points = [current_time_point]
                for _ in range(1, self._proposal_sampling.num_poses + 1, 1):
                    current_time_point += TimePoint(int(0.1 * 1e6))
                    time_points.append(copy.deepcopy(current_time_point))
                
                best_idx = proposal_scores.argmax()
                
                states = state_array_to_ego_states(
                    self._scorer._states[best_idx],
                    time_points,
                    self.ego_state.car_footprint.vehicle_parameters,
                )
                self._trajectory_pdm = InterpolatedTrajectory(states)
                commands = self._simulator.get_commands()
                self._trajectory_pdm.store_commands(commands[best_idx])
                ##################################### PDM #####################################

            
            ##################################### Diffusion #####################################
            if self._iteration % self._diffusion_freq == 0:
                features = self._model_loader.build_features(current_input, self._initialization)
                features['constraints'] = self._generate_constraints(current_input)
                features['counter'] = len(self._frames)
                predictions = self._model_loader._model.run_diffusion(features)


                current_time_point = copy.deepcopy(self.ego_state.time_point)
                time_points = [current_time_point]
                for _ in range(1, self._proposal_sampling.num_poses + 1, 1):
                    current_time_point += TimePoint(int(0.1 * 1e6))
                    time_points.append(copy.deepcopy(current_time_point))

  

                diffusion_proposals_local = predictions["multimodal_trajectories"].detach().cpu().numpy()
                diffusion_proposals_local = diffusion_proposals_local.reshape(-1,16,3)


                # np.concatenate：在轨迹前添加一个全为零的时间步，以便于插值和后续计算。
                diffusion_proposals_local = np.concatenate([np.zeros_like(diffusion_proposals_local[:,:1]), diffusion_proposals_local], axis=1)
                diffusion_proposals_local = interpolate_trajectory(diffusion_proposals_local, 81)
                diffusion_proposals_global = convert_to_global(self.ego_state, diffusion_proposals_local)
                diffusion_proposals_global = np.concatenate([
                    diffusion_proposals_global, np.zeros((diffusion_proposals_global.shape[0], diffusion_proposals_global.shape[1], 8))
                ], axis=2)

                # Simulate
                all_proposals = np.concatenate([pdm_proposals_global,diffusion_proposals_global],axis=0)

                all_states_array = self._simulator.simulate_proposals(all_proposals, self.ego_state) # 仿真器对proposal进行仿真
                
                all_states_scores = self._scorer.score_proposals(
                    all_states_array, # 此处确定的scorer.states
                    self.ego_state,
                    self._observation,
                    self._centerline,
                    self._route_lane_dict,
                    self._drivable_area_map,
                    self._map_api,
                    self._speed_limit,
                    self._lead_agent,
                    state_history,
                )
                all_states_scores = all_states_scores * 100

                pdm_scores = all_states_scores[0:pdm_proposals_global.shape[0]]
                diffusion_scores = all_states_scores[pdm_proposals_global.shape[0]:]

                pdm_idx = pdm_scores.argmax()
                pdm_state_array = all_states_array[pdm_idx]
                diffusion_idx = diffusion_scores.argmax()
                diffusion_state_array = all_states_array[diffusion_idx]

                best_pdm_score = pdm_scores[pdm_idx]
                print("best_pdm_score",best_pdm_score)
                
                best_diffusion_score = diffusion_scores[diffusion_idx]
                print("best_diffusion_score",best_diffusion_score)

                # 加速，可要可不要
                if best_diffusion_score > 98:
                    self._model.predictions_per_sample = int(self._model.predictions_per_sample / 2)
                if best_diffusion_score < 98:
                    self._model.predictions_per_sample = int(self._model.predictions_per_sample * 2)
                self._model.predictions_per_sample = max(min(self._model.predictions_per_sample, 256), 32)

                best_idx = all_states_scores.argmax()
                best_state_array = all_states_array[best_idx]

                states = state_array_to_ego_states(
                all_states_array[pdm_proposals_global.shape[0]+diffusion_idx],
                time_points,
                self.ego_state.car_footprint.vehicle_parameters,
                )
                trajectory = InterpolatedTrajectory(states)
                commands = self._simulator.get_commands()
                trajectory.store_commands(commands[best_idx])
                self._trajectory_diffusion = trajectory

                if best_diffusion_score < 80 and best_pdm_score < 80:
                    self._trapped_count = self._trapped_count+1
                else:
                    self._trapped_count = 0
                    self._trapped = False

                if self._trapped_count >= 5:
                    self._trapped = True
                    print("traaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaped")
            ##################################### Diffusion #####################################
            ####################################### fuse #####################################
                # 对扩散轨迹融合保守轨迹
                # 分数 S_a 和 S_c
                S_a = best_diffusion_score  # 激进轨迹的分数
                S_c = best_pdm_score  # 保守轨迹的分数

                # 设置指数加权的参数
                alpha = 5  # 你可以调节这个值来控制加权效果

                # 计算指数加权后的权重
                w_a = 1 / (1 + np.exp(alpha * (S_c-S_a)))  # 激进轨迹的权重
                w_c = 1 / (1 + np.exp(alpha * (S_a-S_c)))  # 保守轨迹的权重

                # proposal融合
                fused_proposal = pdm_proposals_global[pdm_idx] * w_c + diffusion_proposals_global[diffusion_idx] * w_a
                fused_proposal = np.expand_dims(fused_proposal,axis=0)
                fused_state_array = self._simulator.simulate_proposals(fused_proposal, self.ego_state) # 仿真器对proposal进行仿真
                fused_state_array = np.squeeze(fused_state_array)
                states_fused = state_array_to_ego_states(
                fused_state_array,
                time_points,
                self.ego_state.car_footprint.vehicle_parameters,
                )
                trajectory = InterpolatedTrajectory(states_fused)
                commands = self._simulator.get_commands()
                trajectory.store_commands(commands[0])

                print(w_a)
                print(w_c)
                self._trajectory_diffusion = trajectory
            #########################################select##############################
                # self._planner = "Diffusion"
                # self._diffusion_freq = self._diffusion_freq_high
                # self._pdm_99_count = 0
                # TODO best_diffusion_score 的门限
                # rule1 rule2
                if ((best_diffusion_score > best_pdm_score + self._diffusion_threshold and best_pdm_score > 73 and best_diffusion_score > 73 ) \
                    or best_pdm_score < 73 ) and (self._generator._num_agent_ahead <= 3) or self._trapped == True:
                    self._planner = "Diffusion"
                    self._diffusion_freq = self._diffusion_freq_high
                    if best_diffusion_score > 95:
                        # self._diffusion_count = 1
                        self._diffusion_freq = self._diffusion_freq_low
                        print(self._diffusion_freq)
                        print("ggggggggggggggggggggggggggggggggggggggggggggggggggggggggg")
                    self._pdm_99_count = 0
                    print("超了",best_diffusion_score - best_pdm_score,"分")

                # rule1 rule2
                if (best_pdm_score > best_diffusion_score + self._pdm_threshold and best_pdm_score > 73 and best_diffusion_score > 73 \
                    or (best_diffusion_score < 73 and best_pdm_score > 73)) or self._pdm_99_count >= 6 or self._red_light == True:
                    self._planner = "PDM"
                    self._diffusion_freq = self._diffusion_freq_low
                    # if best_pdm_score < 73:
                    #     self._planner = "Diffusion"
                    #     self._diffusion_freq = self._diffusion_freq_high
                    #     self._pdm_99_count = 0

                ####################### simple score rule #########################
                # if best_diffusion_score > best_pdm_score:
                #     self._planner = "Diffusion"
                #     self._diffusion_freq = self._diffusion_freq_high

                # # rule1 rule2
                # if best_pdm_score > best_diffusion_score:
                #     self._planner = "PDM"
                #     self._diffusion_freq = self._diffusion_freq_low

                ######################## complex score rule ########################
                # if ((best_diffusion_score > best_pdm_score + self._diffusion_threshold and best_pdm_score > 73 and best_diffusion_score > 73 ) \
                #     or best_pdm_score < 73 ):
                #     self._planner = "Diffusion"
                #     self._diffusion_freq = self._diffusion_freq_high
                #     self._pdm_99_count = 0
                #     print("超了",best_diffusion_score - best_pdm_score,"分")

                # # rule1 rule2
                # if (best_pdm_score > best_diffusion_score + self._pdm_threshold and best_pdm_score > 73 and best_diffusion_score > 73 \
                #     or (best_diffusion_score < 73 and best_pdm_score > 73)) :
                #     self._planner = "PDM"
                #     self._diffusion_freq = self._diffusion_freq_low


                ####################### scenario rule ########################
                # if  self._trapped == True:
                #     self._planner = "Diffusion"
                #     self._diffusion_freq = self._diffusion_freq_high
                #     self._pdm_99_count = 0
                #     print("超了",best_diffusion_score - best_pdm_score,"分")

                # # rule1 rule2
                # if self._pdm_99_count >= 6 or self._red_light == True:
                #     self._planner = "PDM"
                #     self._diffusion_freq = self._diffusion_freq_low

                if best_pdm_score > 93:
                    self._pdm_99_count = self._pdm_99_count + 1
                else:
                    self._pdm_99_count = 0
            #########################################select##############################

            if self._planner == "PDM":
                trajectory = self._trajectory_pdm
                print("保守规划器")
            else:
                trajectory = self._trajectory_diffusion
                print("激进规划器")

            self._trajectory = trajectory

            # VIZ
            
            
        
        return self._trajectory
    

    def _generate_constraints(self, current_input):
        """
        Each constraint is a function that maps an ego-trajectory to some scalar cost to be minimized.
        """
        return [
            self._make_pdm_scorer_constraint(current_input, weight=100.0)
        ]
    
    # 高阶函数，里面的函数可以用外面的参数，比如current_input对constraint_fn是可以访问的
    def _make_pdm_scorer_constraint(self, current_input, weight=1.0):
        def constraint_fn(trajectory): # 约束函数返回的是轨迹得分的负值
            device = trajectory.device

            # Convert to scorer format (numpy, 10 Hz, global frame)
            # traj 维度0 轨迹数量 维度1 时间horizon 维度2 自车状态x,y,theta等
            trajectory = trajectory.detach().cpu().numpy()
            trajectory = trajectory.reshape(-1,16,3)
            # np.concatenate：在轨迹前添加一个全为零的时间步，以便于插值和后续计算。
            trajectory = np.concatenate([np.zeros_like(trajectory[:,:1]), trajectory], axis=1)
            trajectory_interp = interpolate_trajectory(trajectory, 81)
            trajectory_global = convert_to_global(self.ego_state, trajectory_interp)
            # TODO 为什么要在后面加8个0 奇怪
            trajectory_padded = np.concatenate([
                trajectory_global, np.zeros((trajectory_global.shape[0], trajectory_global.shape[1], 8))
            ], axis=2)

            # Simulate
            trajectory_sim = self._simulator.simulate_proposals(trajectory_padded, self.ego_state) # 仿真器对proposal进行仿真
            trajectory_sim_local = convert_to_local(self.ego_state, trajectory_sim[...,:3])  # for viz

            # Scoring
            state_history = ego_states_to_state_array(current_input.history.ego_states)
            scores = self._scorer.score_proposals(
                trajectory_sim, # 此处确定的scorer.states
                self.ego_state,
                self._observation,
                self._centerline,
                self._route_lane_dict,
                self._drivable_area_map,
                self._map_api,
                self._speed_limit,
                self._lead_agent,
                state_history,
            )

            scores = -torch.as_tensor(scores, device=device)
            scores = scores * weight
            return scores, {'traj_sim': trajectory_sim_local}

        return constraint_fn

    # Save to gif
    # duration=200：每一帧在 GIF 中显示的时长，单位是毫秒（默认值为 200 毫秒，即每秒 5 帧）。
    def _save_gif(self, fname='', frames=None, duration=200):
        if frames is None:
            frames = self._frames
        if fname == '':
            fname_dir = f'{self._dump_gifs_path}'
            if not os.path.exists(fname_dir):
                os.makedirs(fname_dir)
            fname = f'{fname_dir}/{self._scenario_type}__{self._log_name}__{self._token}__{time.strftime("%m%d-%H%M")}.gif'
        frames = [Image.fromarray(frame) for frame in frames]
        # frames[0].save：从第一帧开始保存 GIF 文件。
        # save_all=True：表示保存整个帧序列，而不是只保存第一帧。
        # append_images=frames[1:]：后续的帧会附加到第一帧之后，构成完整的动画。
        # duration=duration：每一帧显示的时间，单位为毫秒（如 200 毫秒，即 0.2 秒）。
        # loop=0：设置 GIF 动画的循环次数。loop=0 表示 GIF 会无限循环播放。
        frames[0].save(fname, save_all=True, append_images=frames[1:], duration=duration, loop=0)
        print(f'SAVING GIF TO {fname}')

    def _visualize(self, ego_state, features, predictions, mean_point):
        # Viz
        if 'traj_sim' in predictions:
            all_trajectories = predictions['traj_sim']
        else:
            all_trajectories = predictions['multimodal_trajectories'].detach().cpu().numpy()
            all_trajectories = all_trajectories.reshape(-1,16,3)
        scores = predictions['scores'].detach().cpu().numpy()

        # 获取centerline
        centerline = self._centerline.get_nearby_path(self.ego_state, speed_limit=self._speed_limit)
        centerline = np.array(centerline.coords)
        # 转到局部坐标系，以车为中心
        centerline = convert_to_local(ego_state, centerline)

        frame = visualize(
            features['vector_set_map'].to_device('cpu'),
            features['agent_history'].to_device('cpu'),

            centerline=centerline,
            centerline_color='purple',

            alltrajectories=all_trajectories,
            alltrajectories_c=scores,
            alltrajectories_cmap='winter_r',
            alltrajectories_cmin=-110,
            alltrajectories_cmax=0,

            meanpoint=mean_point,
            meanpoint_color='red',

            pixel_size=0.1,
            radius=60,
        )
        return frame

    def _visualize_paper(self, ego_state, features, predictions, save_path):
        # Viz
        if 'traj_sim' in predictions:
            all_trajectories = predictions['traj_sim']
        else:
            all_trajectories = predictions['multimodal_trajectories'].detach().cpu().numpy()
            all_trajectories = all_trajectories.reshape(-1,16,3)
        scores = predictions['scores'].detach().cpu().numpy()

        frame = visualize(
            features['vector_set_map'].to_device('cpu'),
            features['agent_history'].to_device('cpu'),

            alltrajectories=all_trajectories,
            alltrajectories_c=scores,
            alltrajectories_cmap='winter_r',
            alltrajectories_cmin=-100,
            alltrajectories_cmax=0,
            alltrajectories_use_points=False,
            alltrajectories_thickness=3,

            pixel_size=0.05,
            radius=30,
        )

        cv2.imwrite(f'/home/fyq/nuplan/exp/viz_diffusion_es/{save_path}', frame[...,::-1])
        print(f'Saved to {save_path}')
    
    def _visualize_metrics(self, features, predictions):
        if 'traj_sim' in predictions:
            all_trajectories = predictions['traj_sim']
        else:
            all_trajectories = predictions['multimodal_trajectories'].detach().cpu().numpy()
            all_trajectories = all_trajectories.reshape(-1,16,3)
        scores = predictions['scores'].detach().cpu().numpy()

        best_idx = scores.argmin()

        multi_scores = self._scorer._multi_metrics
        weighted_scores = self._scorer._weighted_metrics
        scores_dict = {
            'collision': multi_scores[MultiMetricIndex.NO_COLLISION],
            'drivable_area': multi_scores[MultiMetricIndex.DRIVABLE_AREA],
            'driving_direction': multi_scores[MultiMetricIndex.DRIVING_DIRECTION],
            'speed_limit': multi_scores[MultiMetricIndex.SPEED_LIMIT],

            'progress': weighted_scores[WeightedMetricIndex.PROGRESS],
            'ttc': weighted_scores[WeightedMetricIndex.TTC],
            'comfortable': weighted_scores[WeightedMetricIndex.COMFORTABLE],
            'lane_following': weighted_scores[WeightedMetricIndex.LANE_FOLLOWING],
            'proximity': weighted_scores[WeightedMetricIndex.PROXIMITY]
        }
        frames = []
        for metric in scores_dict:
            frame = visualize(
                features['vector_set_map'].to_device('cpu'),
                features['agent_history'].to_device('cpu'),

                alltrajectories=all_trajectories,
                alltrajectories_c=scores_dict[metric],
                alltrajectories_cmap='winter',
                alltrajectories_cmin=0.0,
                alltrajectories_cmax=1.0,

                pixel_size=0.1,
                radius=25,
            )
            frame2 = visualize(
                features['vector_set_map'].to_device('cpu'),
                features['agent_history'].to_device('cpu'),

                alltrajectories=all_trajectories[best_idx],
                alltrajectories_color='green',

                pixel_size=0.1,
                radius=25,
            )
            frame = np.concatenate([frame, frame2], axis=0)
            frame = cv2.putText(frame, metric, (20,20), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2, cv2.LINE_AA)
            frame = cv2.putText(frame, f't={self._iteration}', (20,60), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2, cv2.LINE_AA)
            frames.append(frame)
        
        return np.concatenate(frames, axis=1)

    def _visualize_collisions(self, fname, scores):
        collision_scores = self._scorer._multi_metrics[MultiMetricIndex.NO_COLLISION]
        active_front_scores = self._scorer._all_collisions[0]
        stopped_track_scores = self._scorer._all_collisions[1]
        active_lateral_scores = self._scorer._all_collisions[2]

        mask1 = active_front_scores == 0
        mask2 = stopped_track_scores == 0
        mask3 = (active_lateral_scores == 0) * (~mask1) * (~mask2)

        colors = colormaps['winter'](collision_scores)
        from shapely import LineString

        # Visualize collision checking
        frames = []
        for time_idx in range(self._scorer._proposal_sampling.num_poses + 1):
            # fig, axs = plt.subplots(2,3, figsize=(30,20))
            fig = plt.figure(figsize=(10,10))
            for i, polygon in enumerate(self._scorer._ego_polygons[:, time_idx]):
                xy = np.stack(polygon.exterior.coords.xy, axis=-1)
                plt.plot(xy[:,0], xy[:,1], color=colors[i])
            for polygon in self._scorer._observation[time_idx]._geometries:
                plt.plot(*polygon.exterior.coords.xy, color='red')
            plt.subplots_adjust(wspace=0, hspace=0)
            frame = fig_to_numpy(fig)
            plt.close()
            frames.append(frame)
        
        # Save gif
        self._save_gif(fname, frames)

    def _visualize_drivable_area(self):
        ego_trajectories = self._scorer._states[:,:,:2]

        fig = plt.figure(figsize=(10,10))
        drivable_area_map = self._scorer._drivable_area_map
        for token in drivable_area_map.tokens:
            poly = drivable_area_map[token]
            color = 'red' if token in self._scorer._route_lane_dict else 'black'
            plt.plot(*poly.exterior.coords.xy, color=color)

        ego_poly = self._scorer._ego_polygons[0, 0]
        ego_xy = np.stack(ego_poly.exterior.coords.xy, axis=-1)
        plt.plot(ego_xy[:,0], ego_xy[:,1], color='green')
        
        drivable_area_scores = self._scorer._multi_metrics[1]
        colors = colormaps['winter'](drivable_area_scores)
        for traj_idx in range(ego_trajectories.shape[0]):
            plt.plot(ego_trajectories[traj_idx,:,0], ego_trajectories[traj_idx,:,1], 
                     c=colors[traj_idx])
        
        plt.axis('off')

        curr_xy = ego_xy[0,:2]
        radius = 40
        plt.xlim(curr_xy[0]-radius,curr_xy[0]+radius)
        plt.ylim(curr_xy[1]-radius,curr_xy[1]+radius)

        frame = fig_to_numpy(fig)
        plt.close(fig)
        return frame
    
    def _visualize_driving_direction(self):
        ego_trajectories = self._scorer._states[:,:,:2]

        fig = plt.figure(figsize=(10,10))
        drivable_area_map = self._scorer._drivable_area_map
        for token in drivable_area_map.tokens:
            poly = drivable_area_map[token]
            color = 'red' if token in self._scorer._route_lane_dict else 'black'
            plt.plot(*poly.exterior.coords.xy, color=color)

        centerline_arr = self._centerline._states_se2_array[...,:2]
        plt.plot(centerline_arr[:,0], centerline_arr[:,1], color='orange')

        for traj_idx in range(ego_trajectories.shape[0]):
            points = ego_trajectories[traj_idx].reshape(-1,2)
            oncoming_traffic_masks = self._scorer._ego_areas[traj_idx, :, 2].flatten().astype(float)   
            colors = colormaps['winter'](oncoming_traffic_masks)
            plt.scatter(points[:,0], points[:,1], c=colors)
        
        plt.axis('off')

        ego_poly = self._scorer._ego_polygons[0, 0]
        ego_xy = np.stack(ego_poly.exterior.coords.xy, axis=-1)
        curr_xy = ego_xy[0,:2]
        radius = 30
        plt.xlim(curr_xy[0]-radius,curr_xy[0]+radius)
        plt.ylim(curr_xy[1]-radius,curr_xy[1]+radius)

        frame = fig_to_numpy(fig)
        plt.close(fig)
        return frame
    
    def _visualize_ids(self, ego_state, features):
        # Parse dynamic objects (vehicles, pedestrians, bikers)
        object_manager = self._observation._object_manager
        dynamic_coords_by_type = {}
        dynamic_tokens_by_type = {}
        dynamic_headings_by_type = {}
        for object_type in object_manager._dynamic_object_coords.keys():
            type_name = object_type.name
            if len(object_manager._dynamic_object_coords[object_type]) > 0:
                coords = np.stack(object_manager._dynamic_object_coords[object_type], axis=0)
                coords = convert_to_local(ego_state, coords)
                tokens = object_manager._dynamic_object_tokens[object_type]

                # Discard faraway objects
                mask = np.linalg.norm(coords[:, BBCoordsIndex.CENTER], axis=-1) < 25.0
                coords = coords[mask]
                tokens = np.array(tokens)[mask]
                headings = [object_manager.unique_objects[token].center.heading for token in tokens]

                dynamic_coords_by_type[type_name] = coords
                dynamic_tokens_by_type[type_name] = tokens
                dynamic_headings_by_type[type_name] = headings

        # Print parsed vehicles
        for type_name in dynamic_coords_by_type:
            for i, coords in enumerate(dynamic_coords_by_type[type_name]):
                center = coords[BBCoordsIndex.CENTER]
                heading = dynamic_headings_by_type[type_name][i]
                print(f'{type_name} {i} at ({center[0]},{center[1]}) at heading {heading:.02f}')

        # Parse lanes
        # There's probably a nicer way to format this, but for now just fetching all nearby objects and doing something hacky
        map_object_types = [SemanticMapLayer.LANE, SemanticMapLayer.LANE_CONNECTOR] # self._map_api.get_available_map_objects()
        map_objects = self._map_api.get_proximal_map_objects(ego_state.center.point, 25.0, layers=map_object_types)
        # map_objects = self._route_lane_dict
        # TODO: aggregate adjacent lanes when possible
        all_map_objects = []
        for map_object_type in map_object_types:
            for map_object in map_objects[map_object_type]:
                # if map_object.id in self._route_lane_dict:
                all_map_objects.append(map_object)

        map_objects = parse_lane_graph(all_map_objects)

        fig = plt.figure(figsize=(20,20))

        for i, node in enumerate(map_objects):
            centerline = convert_to_local(ego_state, node.centerline)
            plt.plot(centerline[:,0], centerline[:,1])
            plt.text(centerline[0,0], centerline[0,1], i)
            print(f'LANE {i} at ({centerline[0,0]},{centerline[0,1]}) to ({centerline[-1,0]},{centerline[-1,1]}) with heading {node.heading:.2f}')

        plt.xlim(-100,100)
        plt.ylim(-100,100)
        plt.savefig('/home/scratch/brianyan/viz/lanes2.png')

        import pdb; pdb.set_trace()

        frame = visualize(
            features['vector_set_map'].to_device('cpu'),
            features['agent_history'].to_device('cpu'),

            pixel_size=0.1,
            radius=60,
        )
        return frame
