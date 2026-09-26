"""海域许可联动领域输入契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed
from .screening import REQUIRED_PLAN_KINDS


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
BOUNDARY_KINDS = {"sea_area", "navigation", "ecology", "maintenance", "cable"}
PLAN_KINDS = {"construction", "export"}
MAX_COORDINATE = 1_000_000_000.0


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def utc_timestamp(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


def _coordinate(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailed(f"{field} 必须是数值坐标")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_COORDINATE:
        raise ValidationFailed(f"{field} 必须是有限且合理的坐标")
    return result


def point_list(value: object, field: str, minimum: int) -> list[list[float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationFailed(f"{field} 必须是坐标数组")
    if len(value) < minimum:
        raise ValidationFailed(f"{field} 至少需要 {minimum} 个坐标点")
    points: list[list[float]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise ValidationFailed(f"{field}[{index}] 必须是 [x, y] 坐标")
        points.append([
            _coordinate(item[0], f"{field}[{index}][0]"),
            _coordinate(item[1], f"{field}[{index}][1]"),
        ])
    return points


def polygon(value: object, field: str) -> list[list[float]]:
    points = point_list(value, field, 3)
    if len({(point[0], point[1]) for point in points}) < 3:
        raise ValidationFailed(f"{field} 至少需要 3 个不同顶点")
    return points


@dataclass(frozen=True, slots=True)
class BoundaryVersionInput:
    version_id: str
    boundary_name: str
    kind: str
    geometry: list[list[float]]
    buffer_m: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BoundaryVersionInput":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in BOUNDARY_KINDS:
            raise ValidationFailed("kind 必须是 sea_area、navigation、ecology、maintenance 或 cable")
        buffer = decimal_value(raw.get("buffer_m", 0), "buffer_m", minimum=Decimal("0"))
        if kind == "cable" and buffer <= 0:
            raise ValidationFailed("既有电缆必须登记大于零的保护距离 buffer_m")
        if kind != "cable" and buffer != 0:
            raise ValidationFailed("只有既有电缆可以登记保护距离 buffer_m")
        return cls(
            version_id=identifier(raw.get("version_id"), "version_id"),
            boundary_name=required_text(raw.get("boundary_name"), "boundary_name", 128),
            kind=kind,
            geometry=polygon(raw.get("geometry"), "geometry"),
            buffer_m=buffer,
        )


@dataclass(frozen=True, slots=True)
class RestrictionWindowInput:
    window_id: str
    boundary_version_id: str
    label: str
    starts_at: str
    ends_at: str
    commitment_expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RestrictionWindowInput":
        starts_at = utc_timestamp(raw.get("starts_at"), "starts_at")
        ends_at = utc_timestamp(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at, "ends_at") <= parse_utc(starts_at, "starts_at"):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            boundary_version_id=identifier(raw.get("boundary_version_id"), "boundary_version_id"),
            label=required_text(raw.get("label"), "label", 128),
            starts_at=starts_at,
            ends_at=ends_at,
            commitment_expires_at=utc_timestamp(raw.get("commitment_expires_at"), "commitment_expires_at"),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationInput:
    authorization_id: str
    boundary_version_id: str
    grantor_party: str
    grantee_party: str
    commitment_expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorizationInput":
        return cls(
            authorization_id=identifier(raw.get("authorization_id"), "authorization_id"),
            boundary_version_id=identifier(raw.get("boundary_version_id"), "boundary_version_id"),
            grantor_party=required_text(raw.get("grantor_party"), "grantor_party", 128),
            grantee_party=required_text(raw.get("grantee_party"), "grantee_party", 128),
            commitment_expires_at=utc_timestamp(raw.get("commitment_expires_at"), "commitment_expires_at"),
        )


def boundary_refs(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValidationFailed("boundary_refs 必须是对象")
    refs = {str(key): identifier(item, f"boundary_refs.{key}") for key, item in value.items()}
    missing = set(REQUIRED_PLAN_KINDS) - set(refs)
    extra = set(refs) - set(REQUIRED_PLAN_KINDS)
    if missing:
        raise ValidationFailed(f"boundary_refs 缺少类别: {','.join(sorted(missing))}")
    if extra:
        raise ValidationFailed(f"boundary_refs 包含未知类别: {','.join(sorted(extra))}")
    return refs


@dataclass(frozen=True, slots=True)
class PlanSegmentInput:
    segment_id: str
    points: list[list[float]]


@dataclass(frozen=True, slots=True)
class PlanInput:
    plan_id: str
    kind: str
    party: str
    title: str
    segments: list[PlanSegmentInput]
    boundary_refs: Mapping[str, str]
    planned_starts_at: str
    planned_ends_at: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanInput":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in PLAN_KINDS:
            raise ValidationFailed("kind 必须是 construction 或 export")
        raw_segments = raw.get("segments")
        if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)) or not raw_segments:
            raise ValidationFailed("segments 必须是非空数组")
        segments: list[PlanSegmentInput] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_segments):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"segments[{index}] 必须是对象")
            segment_id = identifier(item.get("segment_id"), f"segments[{index}].segment_id")
            if segment_id in seen:
                raise ValidationFailed(f"segment_id 重复: {segment_id}")
            seen.add(segment_id)
            segments.append(PlanSegmentInput(
                segment_id=segment_id,
                points=point_list(item.get("points"), f"segments[{index}].points", 2),
            ))
        refs = boundary_refs(raw.get("boundary_refs"))
        starts_at = utc_timestamp(raw.get("planned_starts_at"), "planned_starts_at")
        ends_at = utc_timestamp(raw.get("planned_ends_at"), "planned_ends_at")
        if parse_utc(ends_at, "planned_ends_at") <= parse_utc(starts_at, "planned_starts_at"):
            raise ValidationFailed("planned_ends_at 必须晚于 planned_starts_at")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            kind=kind,
            party=required_text(raw.get("party"), "party", 128),
            title=required_text(raw.get("title"), "title", 256),
            segments=segments,
            boundary_refs=refs,
            planned_starts_at=starts_at,
            planned_ends_at=ends_at,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class BatchInput:
    batch_id: str
    idempotency_key: str
    boundaries: list[BoundaryVersionInput]
    windows: list[RestrictionWindowInput]
    authorizations: list[AuthorizationInput]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BatchInput":
        def items(field: str) -> list[Mapping[str, Any]]:
            value = raw.get(field, [])
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValidationFailed(f"{field} 必须是数组")
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    raise ValidationFailed(f"{field}[{index}] 必须是对象")
            return list(value)

        batch = cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            boundaries=[BoundaryVersionInput.from_dict(item) for item in items("boundaries")],
            windows=[RestrictionWindowInput.from_dict(item) for item in items("windows")],
            authorizations=[AuthorizationInput.from_dict(item) for item in items("authorizations")],
        )
        if not batch.boundaries and not batch.windows and not batch.authorizations:
            raise ValidationFailed("批量导入至少需要一条规则")
        for field, ids in (
            ("boundaries", [item.version_id for item in batch.boundaries]),
            ("windows", [item.window_id for item in batch.windows]),
            ("authorizations", [item.authorization_id for item in batch.authorizations]),
        ):
            if len(ids) != len(set(ids)):
                raise ValidationFailed(f"{field} 内存在重复编号")
        return batch
