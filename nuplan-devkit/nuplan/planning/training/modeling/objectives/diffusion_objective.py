from typing import Dict, List, cast

import torch
import torch.nn.functional as F

from nuplan.planning.training.modeling.objectives.abstract_objective import AbstractObjective
from nuplan.planning.training.modeling.types import FeaturesType, ScenarioListType, TargetsType


class DiffusionObjective(AbstractObjective):
    def __init__(self, scenario_type_loss_weighting: Dict[str, float], weight: float = 1.0):
        pass

    def name(self) -> str:
        """
        Name of the objective
        """
        return 'diffusion_objective'

    def get_list_of_required_target_types(self) -> List[str]:
        """Implemented. See interface."""
        return []

    def compute(self, predictions: FeaturesType, targets: TargetsType, scenarios: ScenarioListType) -> torch.Tensor:
        # 重要：揭示了扩散模型怎么训练的，不断让pred和gt接近！而且targets没有用到！
        # 因为在扩散模型中，预测的不是ego_gt，而是刚开始生成的白噪声，ego_gt只是加噪的初始状态
        if 'epsilon' in predictions:
            pred = predictions['epsilon']
            gt = predictions['gt_epsilon']
            loss = F.mse_loss(pred, gt)
        else:
            # this objective is meaningless at test time
            # 如果在测试阶段未提供 epsilon，则返回一个零值损失。这表明在测试时该目标函数没有意义
            loss = torch.as_tensor(0., device=predictions['trajectory'].data.device)
        return loss
