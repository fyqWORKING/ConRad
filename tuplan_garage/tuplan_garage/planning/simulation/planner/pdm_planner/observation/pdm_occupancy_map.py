from typing import Dict, List

import numpy as np
import numpy.typing as npt
import shapely.vectorized
from nuplan.planning.simulation.occupancy_map.abstract_occupancy_map import Geometry
from shapely.strtree import STRtree

# STRtree 是一种高效的空间查询结构，常用于处理地理信息中的空间查询，比如判断几何对象是否相交或包含某个点等操作。
class PDMOccupancyMap:
    """Occupancy map class of PDM, based on shapely's str-tree."""

    def __init__(
        self,
        tokens: List[str],
        geometries: npt.NDArray[np.object_],
        node_capacity: int = 10,
    ):
        """
        Constructor of PDMOccupancyMap
        :param tokens: list of tracked tokens 注意是tracked tokens
        :param geometries: list/array of polygons
        :param node_capacity: max number of child nodes in str-tree, defaults to 10
        """
        # tokens 和 geometries 应该是一一对应
        assert len(tokens) == len(
            geometries
        ), f"PDMOccupancyMap: Tokens/Geometries ({len(tokens)}/{len(geometries)}) have unequal length!"

        self._tokens: List[str] = tokens
        # 用于将每个 token 映射到其对应的索引位置，便于快速查找。
        # enumerate() 允许你在遍历元素的同时获取其对应的索引值，这在需要知道元素的位置时非常有用
        self._token_to_idx: Dict[str, int] = {
            token: idx for idx, token in enumerate(tokens)
        }

        self._geometries = geometries
        self._node_capacity = node_capacity
        self._str_tree = STRtree(self._geometries, node_capacity)

    def __getitem__(self, token) -> Geometry:
        """
        Retrieves geometry of token.
        :param token: geometry identifier
        :return: Geometry of token
        """
        return self._geometries[self._token_to_idx[token]]

    def __len__(self) -> int:
        """
        Number of geometries in the occupancy map
        :return: int
        """
        return len(self._tokens)

    @property
    def tokens(self) -> List[str]:
        """
        Getter for track tokens in occupancy map
        :return: list of strings
        """
        return self._tokens

    @property
    def token_to_idx(self) -> Dict[str, int]:
        """
        Getter for track tokens in occupancy map
        :return: dictionary of tokens and indices
        """
        return self._token_to_idx

    def intersects(self, geometry: Geometry) -> List[str]:
        """
        Searches for intersecting geometries in the occupancy map
        :param geometry: geometries to query
        :return: list of tokens for intersecting geometries
        """
        indices = self.query(geometry, predicate="intersects")
        return [self._tokens[idx] for idx in indices] # 把索引转化为token:string

    def query(self, geometry: Geometry, predicate=None):
        """
        Function to directly calls shapely's query function on str-tree
        :param geometry: geometries to query
        :param predicate: see shapely, defaults to None
        :return: query output
        """
        return self._str_tree.query(geometry, predicate=predicate)

    def points_in_polygons(
        self, points: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.bool_]:
        """
        Determines wether input-points are in polygons of the occupancy map
        :param points: input-points
        :return: boolean array of shape (polygons, input-points)
        """
        output = np.zeros((len(self._geometries), len(points)), dtype=bool)
        for i, polygon in enumerate(self._geometries):
            output[i] = shapely.vectorized.contains(polygon, points[:, 0], points[:, 1])

        return output
