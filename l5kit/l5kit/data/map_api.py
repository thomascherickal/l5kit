from enum import IntEnum
from functools import lru_cache
from typing import Iterator, Sequence, Union, no_type_check

import numpy as np
import pymap3d as pm

from ..geometry import transform_points
from .proto.road_network_pb2 import GeoFrame, GlobalId, MapElement, MapFragment

CACHE_SIZE = int(1e5)
ENCODING = "utf-8"


class InterpolationMethod(IntEnum):
    INTER_METER = 0  # fixed interpolation at a given step in meters
    INTER_ENSURE_LEN = 1  # ensure we always get the same number of elements


class MapAPI:
    def __init__(self, protobuf_map_path: str, world_to_ecef: np.ndarray):
        """
        Interface to the raw protobuf map file with the following features:
        - access to element using ID is O(1);
        - access to coordinates in world ref system for a set of elements is O(1) after first access (lru cache)
        - object support iteration using __getitem__ protocol

        Args:
            protobuf_map_path (str): path to the protobuf file
            world_to_ecef (np.ndarray): transformation matrix from world coordinates to ECEF (dataset dependent)
        """
        self.protobuf_map_path = protobuf_map_path
        self.ecef_to_world = np.linalg.inv(world_to_ecef)

        with open(protobuf_map_path, "rb") as infile:
            mf = MapFragment()
            mf.ParseFromString(infile.read())

        self.elements = mf.elements
        self.ids_to_el = {self.id_as_str(el.id): idx for idx, el in enumerate(self.elements)}  # store a look-up table

        self.bounds_info = self.get_bounds()  # store bound for semantic elements for fast look-up

    @staticmethod
    @no_type_check
    def id_as_str(element_id: GlobalId) -> str:
        """
        Get the element id as a string.
        Elements ids are stored as a variable len sequence of bytes in the protobuf

        Args:
            element_id (GlobalId): the GlobalId in the protobuf

        Returns:
            str: the id as a str
        """
        return element_id.id.decode(ENCODING)

    @staticmethod
    def _undo_e7(value: float) -> float:
        """
        Latitude and longitude are stored as value*1e7 in the protobuf for efficiency and guaranteed accuracy.
        Convert them back to float.

        Args:
            value (float): the scaled value

        Returns:
            float: the unscaled value
        """
        return value / 1e7

    @no_type_check
    def unpack_deltas_cm(self, dx: Sequence[int], dy: Sequence[int], dz: Sequence[int], frame: GeoFrame) -> np.ndarray:
        """
        Get coords in world reference system (local ENU->ECEF->world).
        See the protobuf annotations for additional information about how coordinates are stored

        Args:
            dx (Sequence[int]): X displacement in centimeters in local ENU
            dy (Sequence[int]): Y displacement in centimeters in local ENU
            dz (Sequence[int]): Z displacement in centimeters in local ENU
            frame (GeoFrame): geo-location information for the local ENU. It contains lat and long origin of the frame

        Returns:
            np.ndarray: array of shape (Nx3) with XYZ coordinates in world ref system

        """
        x = np.cumsum(np.asarray(dx) / 100)
        y = np.cumsum(np.asarray(dy) / 100)
        z = np.cumsum(np.asarray(dz) / 100)
        frame_lat, frame_lng = self._undo_e7(frame.origin.lat_e7), self._undo_e7(frame.origin.lng_e7)
        xyz = np.stack(pm.enu2ecef(x, y, z, frame_lat, frame_lng, 0), axis=-1)
        xyz = transform_points(xyz, self.ecef_to_world)
        return xyz

    @staticmethod
    @no_type_check
    def is_lane(element: MapElement) -> bool:
        """
        Check whether an element is a valid lane

        Args:
            element (MapElement): a proto element

        Returns:
            bool: True if the element is a valid lane
        """
        return bool(element.element.HasField("lane"))

    @lru_cache(maxsize=CACHE_SIZE)
    def get_lane_coords(self, element_id: str) -> dict:
        """
        Get XYZ coordinates in world ref system for a lane given its id
        lru_cached for O(1) access

        Args:
            element_id (str): lane element id

        Returns:
            dict: a dict with the two boundaries coordinates as (Nx3) XYZ arrays
        """
        element = self[element_id]
        assert self.is_lane(element)

        lane = element.element.lane
        left_boundary = lane.left_boundary
        right_boundary = lane.right_boundary

        xyz_left = self.unpack_deltas_cm(
            left_boundary.vertex_deltas_x_cm,
            left_boundary.vertex_deltas_y_cm,
            left_boundary.vertex_deltas_z_cm,
            lane.geo_frame,
        )
        xyz_right = self.unpack_deltas_cm(
            right_boundary.vertex_deltas_x_cm,
            right_boundary.vertex_deltas_y_cm,
            right_boundary.vertex_deltas_z_cm,
            lane.geo_frame,
        )

        return {"xyz_left": xyz_left, "xyz_right": xyz_right}

    @staticmethod
    def interpolate(xyz: np.ndarray, cum_dist: np.ndarray, step: float, method: InterpolationMethod) -> np.ndarray:
        """
        Interpolate points based on cumulative distances from the first one. Two modes are available:
        INTER_METER: interpolate using step as a meter value over cumulative distances (variable len result)
        INTER_ENSURE_LEN: interpolate using a variable step such that we always get step values
        Args:
            xyz (np.ndarray): XYZ coords
            cum_dist (np.ndarray): distances from the first coords of xyz. Same length as xyz
            step (float): param for the interpolation
            method (InterpolationMethod): method to use to interpolate

        Returns:
            np.ndarray: the new interpolated coordinates
        """
        if method == InterpolationMethod.INTER_ENSURE_LEN:
            step = int(step)
            assert step > 1, "step must be at least 2 with INTER_ENSURE_LEN"
            steps = np.linspace(cum_dist[0], cum_dist[-1], step)

        elif method == InterpolationMethod.INTER_METER:
            assert step > 0, "step must be greater than 0 with INTER_FIXED"
            steps = np.arange(cum_dist[0], cum_dist[-1], step)
        else:
            raise NotImplementedError("interpolation method unknown")

        xyz_inter = np.zeros((len(steps), 3), dtype=xyz.dtype)
        xyz_inter[:, 0] = np.interp(steps, xp=cum_dist, fp=xyz[:, 0])
        xyz_inter[:, 1] = np.interp(steps, xp=cum_dist, fp=xyz[:, 1])
        xyz_inter[:, 2] = np.interp(steps, xp=cum_dist, fp=xyz[:, 2])
        return xyz_inter

    @lru_cache(maxsize=CACHE_SIZE)
    def get_lane_traffic_control_ids(self, element_id: str) -> list:
        lane = self[element_id].element.lane
        return [MapAPI.id_as_str(la_tc) for la_tc in lane.traffic_controls]

    @lru_cache(maxsize=CACHE_SIZE)
    def get_lane_as_interpolation(self, element_id: str, step: float, method: InterpolationMethod) -> dict:
        """
        Perform an interpolation of the left and right lanes and compute the midlane.
        See interpolate for details about the different interpolation methods

        Args:
            element_id (str): lane id
            step (float): step param for the method
            method (InterpolationMethod): one of the accepted methods

        Returns:
            dict: same as `get_lane_coords` but overwrite xyz values for the lanes
        """
        lane_dict = self.get_lane_coords(element_id)
        xyz_left = lane_dict["xyz_left"]
        xyz_right = lane_dict["xyz_right"]

        # cumulative distance from the first point, including the first itself as 0
        distances_left = np.cumsum(np.linalg.norm(np.diff(xyz_left, axis=0), axis=-1))
        distances_left = np.insert(distances_left, 0, 0)

        distances_right = np.cumsum(np.linalg.norm(np.diff(xyz_right, axis=0), axis=-1))
        distances_right = np.insert(distances_right, 0, 0)

        lane_dict["xyz_left"] = self.interpolate(xyz_left, distances_left, step, method)
        lane_dict["xyz_right"] = self.interpolate(xyz_right, distances_right, step, method)

        # compute midlane using mean of fixed length lanes
        if method != InterpolationMethod.INTER_ENSURE_LEN:
            mid_steps = max(len(xyz_left), len(xyz_right))
            # recompute lanes using fixed length
            xyz_left = self.interpolate(xyz_left, distances_left, mid_steps, InterpolationMethod.INTER_ENSURE_LEN)
            xyz_right = self.interpolate(xyz_right, distances_right, mid_steps, InterpolationMethod.INTER_ENSURE_LEN)

        else:
            xyz_left = lane_dict["xyz_left"]
            xyz_right = lane_dict["xyz_right"]

        lane_dict["midlane"] = (xyz_left + xyz_right) / 2

        return lane_dict

    @staticmethod
    @no_type_check
    def is_crosswalk(element: MapElement) -> bool:
        """
        Check whether an element is a valid crosswalk

        Args:
            element (MapElement): a proto element

        Returns:
            bool: True if the element is a valid crosswalk
        """
        if not element.element.HasField("traffic_control_element"):
            return False
        traffic_element = element.element.traffic_control_element
        return bool(traffic_element.HasField("pedestrian_crosswalk") and traffic_element.points_x_deltas_cm)

    @lru_cache(maxsize=CACHE_SIZE)
    def get_crosswalk_coords(self, element_id: str) -> dict:
        """
        Get XYZ coordinates in world ref system for a crosswalk given its id
        lru_cached for O(1) access

        Args:
            element_id (str): crosswalk element id

        Returns:
            dict: a dict with the polygon coordinates as an (Nx3) XYZ array
        """
        element = self[element_id]
        assert self.is_crosswalk(element)
        traffic_element = element.element.traffic_control_element

        xyz = self.unpack_deltas_cm(
            traffic_element.points_x_deltas_cm,
            traffic_element.points_y_deltas_cm,
            traffic_element.points_z_deltas_cm,
            traffic_element.geo_frame,
        )

        return {"xyz": xyz}

    def is_traffic_face_colour(self, element_id: str, colour: str) -> bool:
        """
        Check if the element is a traffic light face of the given colour

        Args:
            element_id (str): the id (utf-8 encode) of the element
            colour (str): the colour to check
        Returns:
            True if the element is a traffic light with the given colour
        """
        element = self[element_id]
        if not element.element.HasField("traffic_control_element"):
            return False
        traffic_el = element.element.traffic_control_element
        if (
            traffic_el.HasField(f"signal_{colour}_face")
            or traffic_el.HasField(f"signal_left_arrow_{colour}_face")
            or traffic_el.HasField(f"signal_right_arrow_{colour}_face")
            or traffic_el.HasField(f"signal_upper_left_arrow_{colour}_face")
            or traffic_el.HasField(f"signal_upper_right_arrow_{colour}_face")
        ):
            return True
        return False

    def get_bounds(self) -> dict:
        """
        For each elements of interest returns bounds [[min_x, min_y],[max_x, max_y]] and proto ids
        Coords are computed by the MapAPI and, as such, are in the world ref system.

        Returns:
            dict: keys are classes of elements, values are dict with `bounds` and `ids` keys
        """
        lanes_ids = []
        crosswalks_ids = []

        lanes_bounds = np.empty((0, 2, 2), dtype=np.float)  # [(X_MIN, Y_MIN), (X_MAX, Y_MAX)]
        crosswalks_bounds = np.empty((0, 2, 2), dtype=np.float)  # [(X_MIN, Y_MIN), (X_MAX, Y_MAX)]

        for element in self.elements:
            element_id = MapAPI.id_as_str(element.id)

            if self.is_lane(element):
                lane = self.get_lane_coords(element_id)
                x_min = min(np.min(lane["xyz_left"][:, 0]), np.min(lane["xyz_right"][:, 0]))
                y_min = min(np.min(lane["xyz_left"][:, 1]), np.min(lane["xyz_right"][:, 1]))
                x_max = max(np.max(lane["xyz_left"][:, 0]), np.max(lane["xyz_right"][:, 0]))
                y_max = max(np.max(lane["xyz_left"][:, 1]), np.max(lane["xyz_right"][:, 1]))

                lanes_bounds = np.append(lanes_bounds, np.asarray([[[x_min, y_min], [x_max, y_max]]]), axis=0)
                lanes_ids.append(element_id)

            if self.is_crosswalk(element):
                crosswalk = self.get_crosswalk_coords(element_id)
                x_min = np.min(crosswalk["xyz"][:, 0])
                y_min = np.min(crosswalk["xyz"][:, 1])
                x_max = np.max(crosswalk["xyz"][:, 0])
                y_max = np.max(crosswalk["xyz"][:, 1])

                crosswalks_bounds = np.append(
                    crosswalks_bounds, np.asarray([[[x_min, y_min], [x_max, y_max]]]), axis=0,
                )
                crosswalks_ids.append(element_id)

        return {
            "lanes": {"bounds": lanes_bounds, "ids": lanes_ids},
            "crosswalks": {"bounds": crosswalks_bounds, "ids": crosswalks_ids},
        }

    @no_type_check
    def __getitem__(self, item: Union[int, str, bytes]) -> MapElement:
        if isinstance(item, str):
            return self.elements[self.ids_to_el[item]]
        elif isinstance(item, int):
            return self.elements[item]
        elif isinstance(item, bytes):
            return self.elements[self.ids_to_el[item.decode(ENCODING)]]
        else:
            raise TypeError("only str, bytes and int are allowed in API __getitem__")

    def __len__(self) -> int:
        return len(self.elements)

    def __iter__(self) -> Iterator:
        for i in range(len(self)):
            yield self[i]