"""确定性的平面几何判定,用于候选区生成。

坐标为平面米制坐标,多边形按顶点顺序自动闭合,边界上的点视为内部。
所有函数只依赖输入,不读取时钟或随机源,保证筛查结果可重放。
"""

from __future__ import annotations

import math
from typing import Sequence


Point = tuple[float, float]
EPSILON = 1e-9


def _cross(origin: Point, a: Point, b: Point) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def _edges(ring: Sequence[Point]) -> list[tuple[Point, Point]]:
    return [(ring[index], ring[(index + 1) % len(ring)]) for index in range(len(ring))]


def _on_segment(point: Point, a: Point, b: Point) -> bool:
    if abs(_cross(a, b, point)) > EPSILON:
        return False
    return (
        min(a[0], b[0]) - EPSILON <= point[0] <= max(a[0], b[0]) + EPSILON
        and min(a[1], b[1]) - EPSILON <= point[1] <= max(a[1], b[1]) + EPSILON
    )


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """射线法;落在边界上的点视为内部。"""
    inside = False
    for a, b in _edges(polygon):
        if _on_segment(point, a, b):
            return True
        if (a[1] > point[1]) != (b[1] > point[1]):
            x_cross = a[0] + (point[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x_cross > point[0]:
                inside = not inside
    return inside


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """任意相交判定,包含触碰与共线重叠。"""
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    if ((d1 > EPSILON and d2 < -EPSILON) or (d1 < -EPSILON and d2 > EPSILON)) and (
        (d3 > EPSILON and d4 < -EPSILON) or (d3 < -EPSILON and d4 > EPSILON)
    ):
        return True
    return (
        (abs(d1) <= EPSILON and _on_segment(p1, p3, p4))
        or (abs(d2) <= EPSILON and _on_segment(p2, p3, p4))
        or (abs(d3) <= EPSILON and _on_segment(p3, p1, p2))
        or (abs(d4) <= EPSILON and _on_segment(p4, p1, p2))
    )


def segments_properly_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """两线段在各自内部相交,不含触碰或共线。"""
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    return (
        (d1 > EPSILON and d2 < -EPSILON)
        or (d1 < -EPSILON and d2 > EPSILON)
    ) and (
        (d3 > EPSILON and d4 < -EPSILON)
        or (d3 < -EPSILON and d4 > EPSILON)
    )


def polyline_inside_polygon(points: Sequence[Point], polygon: Sequence[Point]) -> bool:
    """折线整体位于多边形内;允许贴边,不允许穿出。"""
    if not all(point_in_polygon(point, polygon) for point in points):
        return False
    ring = _edges(polygon)
    for a, b in zip(points, points[1:]):
        for c, d in ring:
            if segments_properly_cross(a, b, c, d):
                return False
    return True


def polyline_intersects_polygon(points: Sequence[Point], polygon: Sequence[Point]) -> bool:
    """折线与多边形有任何接触,含进入、穿越或触碰边界。"""
    if any(point_in_polygon(point, polygon) for point in points):
        return True
    ring = _edges(polygon)
    for a, b in zip(points, points[1:]):
        for c, d in ring:
            if segments_intersect(a, b, c, d):
                return True
    return False


def _point_segment_distance(point: Point, a: Point, b: Point) -> float:
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= EPSILON:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    ratio = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq
    ratio = max(0.0, min(1.0, ratio))
    return math.hypot(point[0] - (a[0] + ratio * dx), point[1] - (a[1] + ratio * dy))


def _segment_distance(p1: Point, p2: Point, p3: Point, p4: Point) -> float:
    if segments_intersect(p1, p2, p3, p4):
        return 0.0
    return min(
        _point_segment_distance(p1, p3, p4),
        _point_segment_distance(p2, p3, p4),
        _point_segment_distance(p3, p1, p2),
        _point_segment_distance(p4, p1, p2),
    )


def polyline_polygon_distance(points: Sequence[Point], polygon: Sequence[Point]) -> float:
    """折线到多边形的最短距离;相交、触碰或位于内部时为零。"""
    if polyline_intersects_polygon(points, polygon):
        return 0.0
    ring = _edges(polygon)
    best = math.inf
    for a, b in zip(points, points[1:]):
        for c, d in ring:
            best = min(best, _segment_distance(a, b, c, d))
    return best
