"""确定性的候选区筛查:同时校验海域、航道、生态、运维与既有电缆边界。

本模块只包含纯函数,输入全部来自参数,输出只依赖输入,
因此同一方案在同一组确定版本上的筛查结果可以稳定重放。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .geometry import (
    Point,
    polyline_inside_polygon,
    polyline_intersects_polygon,
    polyline_polygon_distance,
)


ZERO = Decimal("0")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def distance_text(value: float) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), "f")


INCLUSION_KINDS = ("sea_area", "maintenance")
EXCLUSION_KINDS = ("navigation", "ecology")
REQUIRED_PLAN_KINDS = ("sea_area", "navigation", "ecology", "maintenance", "cable")

KIND_LABELS = {
    "sea_area": "海域使用边界",
    "navigation": "航道安全区",
    "ecology": "生态保护边界",
    "maintenance": "运维边界",
    "cable": "既有电缆",
}


def _points(raw_points: Sequence[Sequence[float]]) -> list[Point]:
    return [(float(point[0]), float(point[1])) for point in raw_points]


def _periods_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    left_start = parse_utc(start_a, "starts_at")
    left_end = parse_utc(end_a, "ends_at")
    right_start = parse_utc(start_b, "starts_at")
    right_end = parse_utc(end_b, "ends_at")
    return left_start < right_end and right_start < left_end


def evaluate_segments(
    *,
    segments: Sequence[Mapping[str, Any]],
    versions: Mapping[str, Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    authorizations: Sequence[Mapping[str, Any]],
    party: str,
    planned_starts_at: str,
    planned_ends_at: str,
) -> dict[str, Any]:
    """对每段路线给出准入或排除结论,并记录排除的具体依据。

    versions 按类别固定引用确定版本;windows 为挂在这些版本上的有效限制时段;
    authorizations 为覆盖这些版本且面向方案责任主体的有效授权。
    """
    planned_end = parse_utc(planned_ends_at, "planned_ends_at")
    segment_rows: list[dict[str, Any]] = []
    for segment in segments:
        segment_id = str(segment["segment_id"])
        points = _points(segment["points"])
        findings: list[dict[str, Any]] = []
        for kind in INCLUSION_KINDS:
            version = versions[kind]
            if not polyline_inside_polygon(points, version["geometry"]):
                findings.append({
                    "rule": f"{kind}_outside",
                    "kind": kind,
                    "version_id": version["version_id"],
                    "detail": f"线段超出{KIND_LABELS[kind]} {version['version_id']}({version['boundary_name']})",
                    "points": segment["points"],
                })
        for kind in EXCLUSION_KINDS:
            version = versions[kind]
            if polyline_intersects_polygon(points, version["geometry"]):
                findings.append({
                    "rule": f"{kind}_intersect",
                    "kind": kind,
                    "version_id": version["version_id"],
                    "detail": f"线段穿越{KIND_LABELS[kind]} {version['version_id']}({version['boundary_name']})",
                    "points": segment["points"],
                })
        cable = versions["cable"]
        buffer_m = Decimal(str(cable["buffer_m"]))
        actual = polyline_polygon_distance(points, cable["geometry"])
        if Decimal(str(actual)) < buffer_m:
            findings.append({
                "rule": "cable_buffer",
                "kind": "cable",
                "version_id": cable["version_id"],
                "required_m": decimal_text(buffer_m),
                "actual_m": distance_text(actual),
                "detail": (
                    f"线段与既有电缆 {cable['version_id']}({cable['boundary_name']})最近距离 "
                    f"{distance_text(actual)} 米,低于保护距离 {decimal_text(buffer_m)} 米"
                ),
                "points": segment["points"],
            })
        for window in windows:
            window_geometry = window["geometry"]
            if not _periods_overlap(
                planned_starts_at, planned_ends_at, window["starts_at"], window["ends_at"]
            ):
                continue
            if not polyline_intersects_polygon(points, window_geometry):
                continue
            commitment_end = parse_utc(window["commitment_expires_at"], "commitment_expires_at")
            if commitment_end >= planned_end:
                findings.append({
                    "rule": "restriction_window",
                    "window_id": window["window_id"],
                    "version_id": window["boundary_version_id"],
                    "detail": (
                        f"线段在限制时段 {window['label']}({window['starts_at']} 至 {window['ends_at']})"
                        f"内进入关联边界 {window['boundary_version_id']}"
                    ),
                    "points": segment["points"],
                })
            else:
                findings.append({
                    "rule": "commitment_expired",
                    "window_id": window["window_id"],
                    "version_id": window["boundary_version_id"],
                    "commitment_expires_at": window["commitment_expires_at"],
                    "detail": (
                        f"限制时段 {window['label']} 的协商承诺 {window['commitment_expires_at']} "
                        f"早于计划完工 {planned_ends_at},需重新协商"
                    ),
                    "points": segment["points"],
                })
        segment_rows.append({
            "segment_id": segment_id,
            "status": "excluded" if findings else "admissible",
            "findings": findings,
        })
    plan_findings: list[dict[str, Any]] = []
    for kind in REQUIRED_PLAN_KINDS:
        version = versions[kind]
        covering = [
            item
            for item in authorizations
            if item["boundary_version_id"] == version["version_id"] and item["grantee_party"] == party
        ]
        if not covering:
            plan_findings.append({
                "rule": "authorization_missing",
                "kind": kind,
                "version_id": version["version_id"],
                "party": party,
                "detail": f"责任主体 {party} 缺少版本 {version['version_id']}({KIND_LABELS[kind]})的授权",
            })
            continue
        for item in covering:
            commitment_end = parse_utc(item["commitment_expires_at"], "commitment_expires_at")
            if commitment_end < planned_end:
                plan_findings.append({
                    "rule": "authorization_expired",
                    "kind": kind,
                    "version_id": version["version_id"],
                    "authorization_id": item["authorization_id"],
                    "commitment_expires_at": item["commitment_expires_at"],
                    "detail": (
                        f"授权 {item['authorization_id']} 的承诺 {item['commitment_expires_at']} "
                        f"早于计划完工 {planned_ends_at}"
                    ),
                })
    admissible = not plan_findings and all(row["status"] == "admissible" for row in segment_rows)
    return {
        "admissible": admissible,
        "segments": segment_rows,
        "plan_findings": plan_findings,
        "evaluated_boundary_refs": {kind: versions[kind]["version_id"] for kind in REQUIRED_PLAN_KINDS},
    }
