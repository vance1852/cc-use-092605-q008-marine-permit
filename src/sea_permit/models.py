"""海域许可联动领域输入契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed
from .geometry import Point, close_ring, ring_area


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PLAN_KINDS = {"construction", "export"}
ZONE_CATEGORIES = {"sea_area", "navigation", "ecology", "om"}
CATEGORY_RULES = {
    "sea_area": {"inside", "outside"},
    "navigation": {"outside"},
    "ecology": {"outside"},
    "om": {"inside"},
}
RESTRICTION_KINDS = {"fishery_closure", "navigation_control", "ecology_protection", "other"}
AUTH_SCOPES = {"construction", "export", "both"}


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


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def utc_field(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def decimal_value(value: object, field: str, *, minimum: Decimal | None = None) -> Decimal:
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
    return result


def _points(value: object, field: str, minimum: int) -> list[Point]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValidationFailed(f"{field} 至少需要 {minimum} 个坐标点")
    points: list[Point] = []
    for index, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValidationFailed(f"{field}[{index}] 必须是 [经度, 纬度]")
        lon, lat = item
        for label, coordinate, low, high in (("经度", lon, -180.0, 180.0), ("纬度", lat, -90.0, 90.0)):
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(coordinate)
                or not low <= coordinate <= high
            ):
                raise ValidationFailed(f"{field}[{index}] {label}超出有效范围")
        points.append((float(lon), float(lat)))
    return points


def polygon_ring(value: object, field: str) -> tuple[Point, ...]:
    if not isinstance(value, Mapping) or value.get("type") != "polygon":
        raise ValidationFailed(f"{field} 必须是 polygon 几何对象")
    ring = close_ring(_points(value.get("coordinates"), field, 3))
    if len({point for point in ring}) < 3 or abs(ring_area(ring)) < 1e-12:
        raise ValidationFailed(f"{field} 多边形退化，面积为零")
    return tuple(ring)


def line_path(value: object, field: str) -> tuple[Point, ...]:
    if not isinstance(value, Mapping) or value.get("type") != "line":
        raise ValidationFailed(f"{field} 必须是 line 几何对象")
    return tuple(_points(value.get("coordinates"), field, 2))


def ring_json(ring: tuple[Point, ...]) -> list[list[float]]:
    return [[lon, lat] for lon, lat in ring]


@dataclass(frozen=True, slots=True)
class PermitDraft:
    permit_id: str
    title: str
    responsible_party: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PermitDraft":
        return cls(
            permit_id=identifier(raw.get("permit_id"), "permit_id"),
            title=required_text(raw.get("title"), "title"),
            responsible_party=required_text(raw.get("responsible_party"), "responsible_party"),
        )


@dataclass(frozen=True, slots=True)
class BoundaryDraft:
    boundary: tuple[Point, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BoundaryDraft":
        return cls(boundary=polygon_ring(raw.get("boundary"), "boundary"))


@dataclass(frozen=True, slots=True)
class RestrictionDraft:
    window_id: str
    version: int
    kind: str
    starts_at: str
    ends_at: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RestrictionDraft":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in RESTRICTION_KINDS:
            raise ValidationFailed("kind 不是受支持的限制时段类型")
        starts_at = utc_field(raw.get("starts_at"), "starts_at")
        ends_at = utc_field(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            version=positive_integer(raw.get("version"), "version"),
            kind=kind,
            starts_at=starts_at,
            ends_at=ends_at,
            note=required_text(raw.get("note"), "note"),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationDraft:
    authorization_id: str
    version: int
    party_id: str
    scope: str
    valid_from: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthorizationDraft":
        scope = required_text(raw.get("scope"), "scope", 16)
        if scope not in AUTH_SCOPES:
            raise ValidationFailed("scope 必须是 construction、export 或 both")
        valid_from = utc_field(raw.get("valid_from"), "valid_from")
        valid_until = utc_field(raw.get("valid_until"), "valid_until")
        if valid_until <= valid_from:
            raise ValidationFailed("valid_until 必须晚于 valid_from")
        return cls(
            authorization_id=identifier(raw.get("authorization_id"), "authorization_id"),
            version=positive_integer(raw.get("version"), "version"),
            party_id=identifier(raw.get("party_id"), "party_id"),
            scope=scope,
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class CommitmentDraft:
    commitment_id: str
    version: int
    summary: str
    expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentDraft":
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            version=positive_integer(raw.get("version"), "version"),
            summary=required_text(raw.get("summary"), "summary"),
            expires_at=utc_field(raw.get("expires_at"), "expires_at"),
        )


@dataclass(frozen=True, slots=True)
class ZoneDraft:
    zone_id: str
    category: str
    rule: str
    name: str
    ring: tuple[Point, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ZoneDraft":
        category = required_text(raw.get("category"), "category", 16)
        if category not in ZONE_CATEGORIES:
            raise ValidationFailed("category 必须是 sea_area、navigation、ecology 或 om")
        rule = required_text(raw.get("rule"), "rule", 16)
        if rule not in CATEGORY_RULES[category]:
            allowed = "、".join(sorted(CATEGORY_RULES[category]))
            raise ValidationFailed(f"category 为 {category} 时 rule 只能是 {allowed}")
        return cls(
            zone_id=identifier(raw.get("zone_id"), "zone_id"),
            category=category,
            rule=rule,
            name=required_text(raw.get("name"), "name"),
            ring=polygon_ring(raw.get("geometry"), "geometry"),
        )


@dataclass(frozen=True, slots=True)
class CableDraft:
    cable_id: str
    name: str
    path: tuple[Point, ...]
    protection_distance_m: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CableDraft":
        return cls(
            cable_id=identifier(raw.get("cable_id"), "cable_id"),
            name=required_text(raw.get("name"), "name"),
            path=line_path(raw.get("path"), "path"),
            protection_distance_m=decimal_value(
                raw.get("protection_distance_m"), "protection_distance_m", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class SegmentDraft:
    path: tuple[Point, ...]
    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "SegmentDraft":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        starts_at = utc_field(raw.get("starts_at"), f"{field}.starts_at")
        ends_at = utc_field(raw.get("ends_at"), f"{field}.ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed(f"{field}.ends_at 必须晚于 starts_at")
        return cls(
            path=line_path(raw.get("path"), f"{field}.path"),
            starts_at=starts_at,
            ends_at=ends_at,
        )


@dataclass(frozen=True, slots=True)
class PlanDraft:
    plan_id: str
    kind: str
    title: str
    permit_id: str
    permit_version: int
    party_id: str
    segments: tuple[SegmentDraft, ...]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanDraft":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in PLAN_KINDS:
            raise ValidationFailed("kind 必须是 construction 或 export")
        segments_raw = raw.get("segments")
        if not isinstance(segments_raw, list) or not segments_raw:
            raise ValidationFailed("segments 至少需要一段路线")
        if len(segments_raw) > 64:
            raise ValidationFailed("segments 不能超过 64 段")
        segments = tuple(
            SegmentDraft.from_dict(item, f"segments[{index}]") for index, item in enumerate(segments_raw)
        )
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            kind=kind,
            title=required_text(raw.get("title"), "title"),
            permit_id=identifier(raw.get("permit_id"), "permit_id"),
            permit_version=positive_integer(raw.get("permit_version"), "permit_version"),
            party_id=identifier(raw.get("party_id"), "party_id"),
            segments=segments,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ImportDraft:
    batch_id: str
    zones: tuple[ZoneDraft, ...]
    cables: tuple[CableDraft, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ImportDraft":
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationFailed("items 至少需要一条导入项")
        if len(items) > 256:
            raise ValidationFailed("items 不能超过 256 条")
        zones: list[ZoneDraft] = []
        cables: list[CableDraft] = []
        errors: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                errors.append(f"items[{index}] 必须是对象")
                continue
            kind = item.get("type")
            try:
                if kind == "zone":
                    zones.append(ZoneDraft.from_dict(item))
                elif kind == "cable":
                    cables.append(CableDraft.from_dict(item))
                else:
                    errors.append(f"items[{index}] type 必须是 zone 或 cable")
            except ValidationFailed as exc:
                errors.append(f"items[{index}] {exc}")
        if errors:
            raise ValidationFailed("；".join(errors))
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            zones=tuple(zones),
            cables=tuple(cables),
        )
