import gc  # 垃圾回收模块，用于管理内存。
import logging                               #  用于记录日志。
import warnings                             #  处理警告信息。
from   typing import List,  Optional, Type # 提供类型提示支持。

# 导入的类和函数来自于 nuplan 
from nuplan.planning.simulation.observation.observation_type import (
    DetectionsTracks,
    Observation,
)
from nuplan.planning.simulation.planner.abstract_planner import (
    PlannerInitialization,
    PlannerInput,
)
from nuplan.planning.simulation.trajectory.abstract_trajectory import AbstractTrajectory
from nuplan.planning.simulation.trajectory.trajectory_sampling import TrajectorySampling

# tuplan_garage 库
from tuplan_garage.planning.simulation.planner.pdm_planner.abstract_pdm_closed_planner import (
    AbstractPDMClosedPlanner,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.observation.pdm_observation_utils import (
    get_drivable_area_map,
)
from tuplan_garage.planning.simulation.planner.pdm_planner.proposal.batch_idm_policy import (
    BatchIDMPolicy,
)

warnings.filterwarnings("ignore", category=RuntimeWarning) # 告诉Python解释器忽略所有的RuntimeWarning警告，以免它们在控制台中显示

logger = logging.getLogger(__name__) # 返回当前模块的日志记录器，可以用来记录消息到日志文件或者控制台


class PDMClosedPlanner(AbstractPDMClosedPlanner):
    """PDM-Closed planner class."""

    # Inherited property, see superclass.
    requires_scenario: bool = False

    def __init__(
        self,
        trajectory_sampling: TrajectorySampling,
        proposal_sampling: TrajectorySampling,
        idm_policies: BatchIDMPolicy,
        lateral_offsets: Optional[List[float]],
        map_radius: float,
    ):
        """
        Constructor for PDMClosedPlanner
        :param trajectory_sampling: Sampling parameters for final trajectory
        :param proposal_sampling: Sampling parameters for proposals
        :param idm_policies: BatchIDMPolicy class
        :param lateral_offsets: centerline offsets for proposals (optional)
        :param map_radius: radius around ego to consider
        """
        super(PDMClosedPlanner, self).__init__( # super，调用父类的同名方法
            trajectory_sampling,
            proposal_sampling,
            idm_policies,
            lateral_offsets,
            map_radius,
        )

    # 初始化规划器，设置迭代次数、地图 API 及加载路线字典，并调用垃圾回收
    def initialize(self, initialization: PlannerInitialization) -> None:
        """Inherited, see superclass."""
        self._iteration = 0
        self._map_api = initialization.map_api
        self._load_route_dicts(initialization.route_roadblock_ids)
        gc.collect()

    # 返回类名作为规划器的名称
    def name(self) -> str:
        """Inherited, see superclass."""
        return self.__class__.__name__

    # 返回使用的观测类型 DetectionsTracks
    def observation_type(self) -> Type[Observation]:
        """Inherited, see superclass."""
        return DetectionsTracks  # type: ignore

    def compute_planner_trajectory(
        self, current_input: PlannerInput
    ) -> AbstractTrajectory:
        """Inherited, see superclass."""

        gc.disable() # 禁用垃圾回收，以提高性能
        ego_state, _ = current_input.history.current_state # 获取当前自我状态（ego_state）

        # Apply route correction on first iteration (ego_state required)
        if self._iteration == 0:
            self._route_roadblock_correction(ego_state)

        # Update/Create drivable area polygon map
        self._drivable_area_map = get_drivable_area_map(
            self._map_api, ego_state, self._map_radius # type: ignore
        )

        trajectory = self._get_closed_loop_trajectory(current_input)

        self._iteration += 1
        return trajectory
