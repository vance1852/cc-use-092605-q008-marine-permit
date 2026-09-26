"""确定性的平面几何近似：经纬度按等距圆柱投影换算为米。

平台在单个场区尺度内工作，经度方向按平均纬度缩放，足以支撑
海域边界包含、 exclusion 区相交和既有电缆保护距离判定。
"""

from __future__ import annotations

import math
from typing import Sequence


Point = tuple[float, float]

METERS_PER_DEGREE = 111320.0
_EPS = 1e-9


def close_ring(points: Sequence[Point]) -> list[Point]:
    ring = [(float(lon), float(lat)) for lon, lat in points]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def ring_area(ring: Sequence[Point]) -> float:
    area = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _orientation(a: Point, b: Point, c: Point) -> int:
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if cross > _EPS:
        return 1
    if cross < -_EPS:
        return -1
    return 0


def point_on_segment(p: Point, a: Point, b: Point) -> bool:
    if _orientation(a, b, p) != 0:
        return False
    return (
        min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
        and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS
    )


def segments_touch(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """两条线段存在任意接触（相交、端点接触或共线重叠）。"""
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if o1 == 0 and point_on_segment(b1, a1, a2):
        return True
    if o2 == 0 and point_on_segment(b2, a1, a2):
        return True
    if o3 == 0 and point_on_segment(a1, b1, b2):
        return True
    if o4 == 0 and point_on_segment(a2, b1, b2):
        return True
    return False


def _strict_cross(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """两条线段在各自内部相交（不含端点接触）。"""
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)
    return o1 * o2 < 0 and o3 * o4 < 0


def point_in_ring(point: Point, ring: Sequence[Point]) -> bool:
    """射线法，边界上的点视为内部。"""
    for index in range(len(ring) - 1):
        if point_on_segment(point, ring[index], ring[index + 1]):
            return True
    x, y = point
    inside = False
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        if (y1 > y) != (y2 > y):
            xinters = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if xinters > x:
                inside = not inside
    return inside


def segment_inside_ring(a: Point, b: Point, ring: Sequence[Point]) -> bool:
    """线段完整落在多边形内（允许贴合边界）。"""
    midpoint = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    if not point_in_ring(a, ring) or not point_in_ring(b, ring) or not point_in_ring(midpoint, ring):
        return False
    for index in range(len(ring) - 1):
        if _strict_cross(a, b, ring[index], ring[index + 1]):
            return False
    return True


def segment_touches_ring(a: Point, b: Point, ring: Sequence[Point]) -> bool:
    """线段与多边形存在任意接触（进入内部或触碰边界）。"""
    midpoint = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    if point_in_ring(a, ring) or point_in_ring(b, ring) or point_in_ring(midpoint, ring):
        return True
    for index in range(len(ring) - 1):
        if segments_touch(a, b, ring[index], ring[index + 1]):
            return True
    return False


def path_inside_ring(path: Sequence[Point], ring: Sequence[Point]) -> bool:
    return all(segment_inside_ring(path[i], path[i + 1], ring) for i in range(len(path) - 1))


def path_touches_ring(path: Sequence[Point], ring: Sequence[Point]) -> bool:
    return any(segment_touches_ring(path[i], path[i + 1], ring) for i in range(len(path) - 1))


def _project(point: Point, latitude0: float) -> Point:
    return (
        point[0] * METERS_PER_DEGREE * math.cos(math.radians(latitude0)),
        point[1] * METERS_PER_DEGREE,
    )


def _point_segment_distance_m(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def segment_distance_m(a1: Point, a2: Point, b1: Point, b2: Point) -> float:
    """两条线段的最短距离（米），相交或接触时为零。"""
    if segments_touch(a1, a2, b1, b2):
        return 0.0
    latitude0 = (a1[1] + a2[1] + b1[1] + b2[1]) / 4.0
    pa1, pa2, pb1, pb2 = (
        _project(a1, latitude0),
        _project(a2, latitude0),
        _project(b1, latitude0),
        _project(b2, latitude0),
    )
    return min(
        _point_segment_distance_m(pa1, pb1, pb2),
        _point_segment_distance_m(pa2, pb1, pb2),
        _point_segment_distance_m(pb1, pa1, pa2),
        _point_segment_distance_m(pb2, pa1, pa2),
    )


def path_distance_m(path_a: Sequence[Point], path_b: Sequence[Point]) -> float:
    """两条折线的最短距离（米）。"""
    best = math.inf
    for i in range(len(path_a) - 1):
        for j in range(len(path_b) - 1):
            best = min(best, segment_distance_m(path_a[i], path_a[i + 1], path_b[j], path_b[j + 1]))
    return best
