"""海域许可联动、方案评估、许可撤回与人工处置的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .geometry import Point
from .geometry import path_distance_m, path_inside_ring, path_touches_ring
from .models import (
    AuthorizationDraft,
    BoundaryDraft,
    CableDraft,
    CommitmentDraft,
    ImportDraft,
    PermitDraft,
    PlanDraft,
    RestrictionDraft,
    ZoneDraft,
    required_text,
    ring_json,
)
from .planning import canonical_json, decimal_text, digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "constructor": {"plan.write", "plan.evaluate", "plan.confirm", "view.construction", "constraint.read"},
    "operator": {"plan.execute", "view.om", "constraint.read"},
    "regulator": {
        "permit.write",
        "permit.revoke",
        "permit.read",
        "import.write",
        "manual.resolve",
        "constraint.read",
    },
    "auditor": {"view.audit", "audit.read", "permit.read", "constraint.read"},
}

PLAN_VIEW_BY_ROLE = {
    "constructor": "construction",
    "operator": "om",
    "auditor": "audit",
}


class PermitService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---------- 基础辅助 ----------

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM sea_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM sea_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO sea_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sea_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def _permit(self, permit_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM sea_permits WHERE permit_id=?", (permit_id,)
        ).fetchone()
        if row is None:
            raise NotFound("海域许可不存在")
        return row

    def _version_row(self, permit_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM permit_versions WHERE permit_id=? AND version=?",
            (permit_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("许可边界版本不存在")
        return row

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM sea_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    # ---------- 许可登记 ----------

    def create_permit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "permit.write")
        draft = PermitDraft.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sea_permits(permit_id,title,responsible_party,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (draft.permit_id, draft.title, draft.responsible_party, actor_id, self._now()),
                )
                self._audit("permit", draft.permit_id, "permit.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("许可编号已经存在") from exc
        return {"permit_id": draft.permit_id, "state": "active"}

    def register_boundary_version(self, actor_id: str, permit_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "permit.write")
        permit = self._permit(permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，不能登记新边界版本")
        draft = BoundaryDraft.from_dict(raw)
        boundary_json = canonical_json(ring_json(draft.boundary))
        content_sha256 = hashlib.sha256(boundary_json.encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM permit_versions WHERE permit_id=?",
            (permit_id,),
        ).fetchone()
        version = int(row["next_version"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO permit_versions(permit_id,version,boundary_json,content_sha256,"
                    "registered_by,registered_at) VALUES(?,?,?,?,?,?)",
                    (permit_id, version, boundary_json, content_sha256, actor_id, self._now()),
                )
                self._audit(
                    "permit",
                    permit_id,
                    "permit.version_registered",
                    actor_id,
                    {"version": version, "content_sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("相同边界内容已登记为既有版本") from exc
        return {"permit_id": permit_id, "version": version, "content_sha256": content_sha256}

    def register_restriction(self, actor_id: str, permit_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "permit.write")
        permit = self._permit(permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，不能登记限制时段")
        draft = RestrictionDraft.from_dict(raw)
        self._version_row(permit_id, draft.version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO restriction_windows(window_id,permit_id,version,kind,starts_at,ends_at,"
                    "note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.window_id,
                        permit_id,
                        draft.version,
                        draft.kind,
                        draft.starts_at,
                        draft.ends_at,
                        draft.note,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "permit",
                    permit_id,
                    "permit.restriction_registered",
                    actor_id,
                    {"window_id": draft.window_id, "version": draft.version, "kind": draft.kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("限制时段编号已经存在") from exc
        return {"window_id": draft.window_id, "permit_id": permit_id, "version": draft.version}

    def grant_authorization(self, actor_id: str, permit_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "permit.write")
        permit = self._permit(permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，不能登记责任主体授权")
        draft = AuthorizationDraft.from_dict(raw)
        self._version_row(permit_id, draft.version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO party_authorizations(authorization_id,permit_id,version,party_id,scope,"
                    "valid_from,valid_until,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.authorization_id,
                        permit_id,
                        draft.version,
                        draft.party_id,
                        draft.scope,
                        draft.valid_from,
                        draft.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "permit",
                    permit_id,
                    "permit.authorization_granted",
                    actor_id,
                    {
                        "authorization_id": draft.authorization_id,
                        "version": draft.version,
                        "party_id": draft.party_id,
                        "scope": draft.scope,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号已经存在") from exc
        return {"authorization_id": draft.authorization_id, "permit_id": permit_id, "version": draft.version}

    def register_commitment(self, actor_id: str, permit_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "permit.write")
        permit = self._permit(permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，不能登记承诺")
        draft = CommitmentDraft.from_dict(raw)
        self._version_row(permit_id, draft.version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO permit_commitments(commitment_id,permit_id,version,summary,expires_at,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        draft.commitment_id,
                        permit_id,
                        draft.version,
                        draft.summary,
                        draft.expires_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "permit",
                    permit_id,
                    "permit.commitment_registered",
                    actor_id,
                    {
                        "commitment_id": draft.commitment_id,
                        "version": draft.version,
                        "expires_at": draft.expires_at,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("承诺编号已经存在") from exc
        return {"commitment_id": draft.commitment_id, "permit_id": permit_id, "version": draft.version}

    # ---------- 批量导入 ----------

    def import_constraints(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "import.write")
        draft = ImportDraft.from_dict(raw)
        request_sha256 = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM import_batches WHERE batch_id=?",
            (draft.batch_id,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("批次编号对应不同导入内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        response = {
            "batch_id": draft.batch_id,
            "item_count": len(draft.zones) + len(draft.cables),
            "zones": [zone.zone_id for zone in draft.zones],
            "cables": [cable.cable_id for cable in draft.cables],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO import_batches(batch_id,request_sha256,response_json,item_count,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        draft.batch_id,
                        request_sha256,
                        canonical_json(response),
                        response["item_count"],
                        actor_id,
                        self._now(),
                    ),
                )
                for zone in draft.zones:
                    self.connection.execute(
                        "INSERT INTO constraint_zones(zone_id,category,rule,name,geometry_json,"
                        "source_batch_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            zone.zone_id,
                            zone.category,
                            zone.rule,
                            zone.name,
                            canonical_json(ring_json(zone.ring)),
                            draft.batch_id,
                            actor_id,
                            self._now(),
                        ),
                    )
                for cable in draft.cables:
                    self.connection.execute(
                        "INSERT INTO cable_corridors(cable_id,name,path_json,protection_distance_m,"
                        "source_batch_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            cable.cable_id,
                            cable.name,
                            canonical_json(ring_json(cable.path)),
                            decimal_text(cable.protection_distance_m),
                            draft.batch_id,
                            actor_id,
                            self._now(),
                        ),
                    )
                self._audit(
                    "batch",
                    draft.batch_id,
                    "batch.imported",
                    actor_id,
                    {"item_count": response["item_count"], "zones": response["zones"], "cables": response["cables"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号或约束编号冲突") from exc
        return {**response, "replayed": False}

    # ---------- 方案登记与评估 ----------

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        draft = PlanDraft.from_dict(raw)
        request_sha256 = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM sea_idempotency WHERE scope='plan' AND idempotency_key=?",
            (draft.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("幂等键对应不同方案内容")
            return json.loads(stored["response_json"])
        permit = self._permit(draft.permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，不能登记方案")
        self._version_row(draft.permit_id, draft.permit_version)
        response = {
            "plan_id": draft.plan_id,
            "kind": draft.kind,
            "permit_id": draft.permit_id,
            "permit_version": draft.permit_version,
            "state": "draft",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sea_plans(plan_id,kind,title,permit_id,permit_version,party_id,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.plan_id,
                        draft.kind,
                        draft.title,
                        draft.permit_id,
                        draft.permit_version,
                        draft.party_id,
                        draft.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                for index, segment in enumerate(draft.segments):
                    self.connection.execute(
                        "INSERT INTO plan_segments(plan_id,segment_index,path_json,starts_at,ends_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            draft.plan_id,
                            index,
                            canonical_json(ring_json(segment.path)),
                            segment.starts_at,
                            segment.ends_at,
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO sea_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('plan',?,?,?,?)",
                    (draft.idempotency_key, request_sha256, canonical_json(response), self._now()),
                )
                self._audit(
                    "plan",
                    draft.plan_id,
                    "plan.created",
                    actor_id,
                    {
                        "kind": draft.kind,
                        "permit_id": draft.permit_id,
                        "permit_version": draft.permit_version,
                        "segments": len(draft.segments),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号或幂等键冲突") from exc
        return response

    @staticmethod
    def _parse_path(path_json: str) -> list[Point]:
        return [(float(lon), float(lat)) for lon, lat in json.loads(path_json)]

    def _evaluate_segment(
        self,
        *,
        plan: sqlite3.Row,
        segment: sqlite3.Row,
        permit: sqlite3.Row,
        boundary: Sequence[Point],
        zones: Sequence[sqlite3.Row],
        cables: Sequence[sqlite3.Row],
        windows: Sequence[sqlite3.Row],
        authorizations: Sequence[sqlite3.Row],
        commitments: Sequence[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        if permit["state"] != "active":
            reasons.append(
                {
                    "code": "permit_revoked",
                    "category": "permit",
                    "reference_id": permit["permit_id"],
                    "message": f"许可 {permit['permit_id']} 已撤回，方案失去合法依据",
                }
            )
            return reasons
        path = self._parse_path(segment["path_json"])
        if not path_inside_ring(path, boundary):
            reasons.append(
                {
                    "code": "outside_boundary",
                    "category": "sea_area",
                    "reference_id": f"{permit['permit_id']}@v{plan['permit_version']}",
                    "message": "线段未完整落在海域使用边界之内",
                }
            )
        inside_zones: dict[str, list[sqlite3.Row]] = {"sea_area": [], "om": []}
        for zone in zones:
            ring = self._parse_path(zone["geometry_json"])
            if zone["rule"] == "outside":
                if path_touches_ring(path, ring):
                    reasons.append(
                        {
                            "code": "inside_exclusion",
                            "category": zone["category"],
                            "reference_id": zone["zone_id"],
                            "message": f"线段进入{zone['name']}（{zone['category']} 类排除区）",
                        }
                    )
            else:
                inside_zones.setdefault(zone["category"], []).append(zone)
        for category in ("sea_area", "om"):
            candidates = inside_zones.get(category, [])
            if category == "sea_area" and not candidates:
                continue
            if not candidates:
                reasons.append(
                    {
                        "code": "boundary_missing",
                        "category": category,
                        "reference_id": category,
                        "message": "未登记运维边界，无法确认线段满足运维边界要求",
                    }
                )
                continue
            if not any(path_inside_ring(path, self._parse_path(zone["geometry_json"])) for zone in candidates):
                reasons.append(
                    {
                        "code": "outside_boundary",
                        "category": category,
                        "reference_id": "+".join(zone["zone_id"] for zone in candidates),
                        "message": f"线段未完整落在任一{category}类边界之内",
                    }
                )
        for cable in cables:
            distance = path_distance_m(path, self._parse_path(cable["path_json"]))
            protection = Decimal(cable["protection_distance_m"])
            if Decimal(str(distance)) < protection:
                reasons.append(
                    {
                        "code": "cable_protection",
                        "category": "cable",
                        "reference_id": cable["cable_id"],
                        "message": (
                            f"线段与既有电缆 {cable['name']} 的最短距离约 "
                            f"{round(distance, 3)} 米，小于保护距离 {decimal_text(protection)} 米"
                        ),
                    }
                )
        for window in windows:
            if segment["starts_at"] < window["ends_at"] and segment["ends_at"] > window["starts_at"]:
                reasons.append(
                    {
                        "code": "restriction_window",
                        "category": "time",
                        "reference_id": window["window_id"],
                        "message": (
                            f"施工窗口与限制时段 {window['kind']}"
                            f"（{window['starts_at']} 至 {window['ends_at']}）相交"
                        ),
                    }
                )
        authorized = any(
            row["party_id"] == plan["party_id"]
            and row["scope"] in (plan["kind"], "both")
            and row["valid_from"] <= segment["starts_at"]
            and row["valid_until"] >= segment["ends_at"]
            for row in authorizations
        )
        if not authorized:
            reasons.append(
                {
                    "code": "authorization_missing",
                    "category": "authorization",
                    "reference_id": plan["party_id"],
                    "message": f"责任主体 {plan['party_id']} 没有覆盖该施工窗口与方案类型的有效授权",
                }
            )
        if not commitments:
            reasons.append(
                {
                    "code": "commitment_missing",
                    "category": "commitment",
                    "reference_id": permit["permit_id"],
                    "message": "该许可版本未登记承诺到期时间",
                }
            )
        else:
            earliest = min(row["expires_at"] for row in commitments)
            if segment["ends_at"] > earliest:
                reasons.append(
                    {
                        "code": "commitment_expired",
                        "category": "commitment",
                        "reference_id": permit["permit_id"],
                        "message": f"施工窗口结束时间晚于承诺到期时间 {earliest}",
                    }
                )
        return reasons

    def evaluate_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.evaluate")
        plan = self._plan(plan_id)
        if plan["state"] != "draft":
            raise InvalidState("只有草稿方案可以评估")
        permit = self._permit(plan["permit_id"])
        version_row = self._version_row(plan["permit_id"], plan["permit_version"])
        boundary = self._parse_path(version_row["boundary_json"])
        zones = self.connection.execute(
            "SELECT * FROM constraint_zones WHERE active=1 ORDER BY category,zone_id"
        ).fetchall()
        cables = self.connection.execute(
            "SELECT * FROM cable_corridors WHERE active=1 ORDER BY cable_id"
        ).fetchall()
        windows = self.connection.execute(
            "SELECT * FROM restriction_windows WHERE permit_id=? AND version=? ORDER BY window_id",
            (plan["permit_id"], plan["permit_version"]),
        ).fetchall()
        authorizations = self.connection.execute(
            "SELECT * FROM party_authorizations WHERE permit_id=? AND version=? ORDER BY authorization_id",
            (plan["permit_id"], plan["permit_version"]),
        ).fetchall()
        commitments = self.connection.execute(
            "SELECT * FROM permit_commitments WHERE permit_id=? AND version=? ORDER BY commitment_id",
            (plan["permit_id"], plan["permit_version"]),
        ).fetchall()
        segments = self.connection.execute(
            "SELECT * FROM plan_segments WHERE plan_id=? ORDER BY segment_index", (plan_id,)
        ).fetchall()
        evaluated_at = self._now()
        candidates: list[int] = []
        excluded: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for segment in segments:
                reasons = self._evaluate_segment(
                    plan=plan,
                    segment=segment,
                    permit=permit,
                    boundary=boundary,
                    zones=zones,
                    cables=cables,
                    windows=windows,
                    authorizations=authorizations,
                    commitments=commitments,
                )
                verdict = "candidate" if not reasons else "excluded"
                input_sha256 = digest(
                    {
                        "plan_id": plan_id,
                        "segment_index": segment["segment_index"],
                        "permit_id": plan["permit_id"],
                        "permit_version": plan["permit_version"],
                        "boundary_sha256": version_row["content_sha256"],
                        "permit_state": permit["state"],
                        "segment": {
                            "path": json.loads(segment["path_json"]),
                            "starts_at": segment["starts_at"],
                            "ends_at": segment["ends_at"],
                        },
                        "zones": [dict(row) for row in zones],
                        "cables": [dict(row) for row in cables],
                        "windows": [dict(row) for row in windows],
                        "authorizations": [dict(row) for row in authorizations],
                        "commitments": [dict(row) for row in commitments],
                    }
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO segment_evaluations(plan_id,segment_index,permit_id,permit_version,"
                    "verdict,reasons_json,input_sha256,evaluated_by,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        segment["segment_index"],
                        plan["permit_id"],
                        plan["permit_version"],
                        verdict,
                        canonical_json(reasons),
                        input_sha256,
                        actor_id,
                        evaluated_at,
                    ),
                )
                self.connection.execute(
                    "UPDATE plan_segments SET state=? WHERE plan_id=? AND segment_index=?",
                    (verdict, plan_id, segment["segment_index"]),
                )
                if reasons:
                    excluded.append({"segment_index": segment["segment_index"], "reasons": reasons})
                else:
                    candidates.append(segment["segment_index"])
            self._audit(
                "plan",
                plan_id,
                "plan.evaluated",
                actor_id,
                {
                    "permit_version": plan["permit_version"],
                    "candidates": candidates,
                    "excluded": [item["segment_index"] for item in excluded],
                },
            )
        return {
            "plan_id": plan_id,
            "permit_version": plan["permit_version"],
            "candidates": candidates,
            "excluded": excluded,
            "evaluated_at": evaluated_at,
        }

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan = self._plan(plan_id)
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前草稿版本")
        permit = self._permit(plan["permit_id"])
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，方案不能确认")
        segments = self.connection.execute(
            "SELECT segment_index FROM plan_segments WHERE plan_id=? ORDER BY segment_index", (plan_id,)
        ).fetchall()
        evaluations = self.connection.execute(
            "SELECT segment_index,verdict FROM segment_evaluations "
            "WHERE plan_id=? AND permit_id=? AND permit_version=?",
            (plan_id, plan["permit_id"], plan["permit_version"]),
        ).fetchall()
        verdicts = {row["segment_index"]: row["verdict"] for row in evaluations}
        if len(verdicts) != len(segments):
            raise InvalidState("方案尚未完成评估")
        blocked = [index for index, verdict in sorted(verdicts.items()) if verdict != "candidate"]
        if blocked:
            raise InvalidState(f"线段 {blocked} 被排除，方案不能确认")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE sea_plans SET state='confirmed',revision=revision+1 "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前草稿版本")
            self._audit("plan", plan_id, "plan.confirmed", actor_id, {"permit_version": plan["permit_version"]})
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    # ---------- 方案执行 ----------

    def start_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.execute")
        plan = self._plan(plan_id)
        if plan["state"] != "confirmed" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前已确认版本")
        permit = self._permit(plan["permit_id"])
        if permit["state"] != "active":
            raise InvalidState("许可已撤回，方案不能开工")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE sea_plans SET state='in_progress',revision=revision+1 "
                "WHERE plan_id=? AND state='confirmed' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前已确认版本")
            self._audit("plan", plan_id, "plan.started", actor_id, {})
        return {"plan_id": plan_id, "state": "in_progress", "revision": expected_revision + 1}

    def complete_segment(self, actor_id: str, plan_id: str, segment_index: int) -> dict[str, Any]:
        self._require(actor_id, "plan.execute")
        plan = self._plan(plan_id)
        if plan["state"] != "in_progress":
            raise InvalidState("方案不在执行中")
        segment = self.connection.execute(
            "SELECT * FROM plan_segments WHERE plan_id=? AND segment_index=?",
            (plan_id, segment_index),
        ).fetchone()
        if segment is None:
            raise NotFound("方案线段不存在")
        if segment["state"] != "candidate":
            raise InvalidState("只有候选线段可以登记完成")
        completed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE plan_segments SET state='completed',completed_at=? "
                "WHERE plan_id=? AND segment_index=? AND state='candidate'",
                (completed_at, plan_id, segment_index),
            )
            self._audit(
                "plan",
                plan_id,
                "plan.segment_completed",
                actor_id,
                {"segment_index": segment_index, "completed_at": completed_at},
            )
        return {"plan_id": plan_id, "segment_index": segment_index, "state": "completed", "completed_at": completed_at}

    def complete_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.execute")
        plan = self._plan(plan_id)
        if plan["state"] != "in_progress" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前执行中版本")
        remaining = self.connection.execute(
            "SELECT COUNT(*) AS pending FROM plan_segments WHERE plan_id=? AND state<>'completed'",
            (plan_id,),
        ).fetchone()
        if remaining["pending"]:
            raise InvalidState("尚有线段未完成，方案不能完工")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE sea_plans SET state='completed',revision=revision+1 "
                "WHERE plan_id=? AND state='in_progress' AND revision=?",
                (plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前执行中版本")
            self._audit("plan", plan_id, "plan.completed", actor_id, {})
        return {"plan_id": plan_id, "state": "completed", "revision": expected_revision + 1}

    # ---------- 许可撤回与人工处置 ----------

    def revoke_permit(self, actor_id: str, permit_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "permit.revoke")
        permit = self._permit(permit_id)
        if permit["state"] != "active":
            raise InvalidState("许可已撤回")
        reason_text = required_text(reason, "reason")
        revoked_at = self._now()
        blocked: list[str] = []
        manual_case_ids: list[int] = []
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE sea_permits SET state='revoked',revoke_reason=?,revoked_by=?,revoked_at=? "
                "WHERE permit_id=? AND state='active'",
                (reason_text, actor_id, revoked_at, permit_id),
            )
            plans = self.connection.execute(
                "SELECT plan_id,state FROM sea_plans WHERE permit_id=? AND state IN "
                "('draft','confirmed','in_progress') ORDER BY plan_id",
                (permit_id,),
            ).fetchall()
            for plan in plans:
                if plan["state"] == "draft":
                    self.connection.execute(
                        "UPDATE sea_plans SET state='blocked',revision=revision+1 WHERE plan_id=? AND state='draft'",
                        (plan["plan_id"],),
                    )
                    blocked.append(plan["plan_id"])
                    self._audit(
                        "plan",
                        plan["plan_id"],
                        "plan.blocked",
                        actor_id,
                        {"trigger": "permit_revoked", "permit_id": permit_id},
                    )
                else:
                    self.connection.execute(
                        "UPDATE sea_plans SET state='manual_review',revision=revision+1 "
                        "WHERE plan_id=? AND state=?",
                        (plan["plan_id"], plan["state"]),
                    )
                    cursor = self.connection.execute(
                        "INSERT INTO manual_cases(plan_id,trigger,prior_state,created_at) VALUES(?,?,?,?)",
                        (plan["plan_id"], "permit_revoked", plan["state"], revoked_at),
                    )
                    case_id = int(cursor.lastrowid)
                    manual_case_ids.append(case_id)
                    self._audit(
                        "plan",
                        plan["plan_id"],
                        "plan.manual_review",
                        actor_id,
                        {"trigger": "permit_revoked", "permit_id": permit_id, "case_id": case_id},
                    )
            self._audit(
                "permit",
                permit_id,
                "permit.revoked",
                actor_id,
                {"reason": reason_text, "blocked_plans": blocked, "manual_cases": manual_case_ids},
            )
        return {
            "permit_id": permit_id,
            "state": "revoked",
            "revoked_at": revoked_at,
            "blocked_plan_ids": blocked,
            "manual_case_ids": manual_case_ids,
        }

    def resolve_manual_case(self, actor_id: str, case_id: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "manual.resolve")
        case = self.connection.execute(
            "SELECT * FROM manual_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFound("人工处置单不存在")
        if case["state"] != "open":
            raise InvalidState("人工处置单已办结")
        plan = self._plan(case["plan_id"])
        action = required_text(raw.get("action"), "action", 16)
        if action not in {"halt", "repin"}:
            raise ValidationFailed("action 必须是 halt 或 repin")
        note = required_text(raw.get("note"), "note")
        resolved_at = self._now()
        with transaction(self.connection, immediate=True):
            if action == "halt":
                cursor = self.connection.execute(
                    "UPDATE sea_plans SET state='blocked',revision=revision+1 "
                    "WHERE plan_id=? AND state='manual_review'",
                    (plan["plan_id"],),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("方案不在人工处置状态")
                new_state = "blocked"
                self._audit("plan", plan["plan_id"], "plan.halted", actor_id, {"case_id": case_id, "note": note})
            else:
                target_permit_id = required_text(raw.get("permit_id"), "permit_id", 64)
                target_version = raw.get("permit_version")
                if isinstance(target_version, bool) or not isinstance(target_version, int) or target_version <= 0:
                    raise ValidationFailed("permit_version 必须是正整数")
                target = self._permit(target_permit_id)
                if target["state"] != "active":
                    raise InvalidState("目标许可不可用，不能迁移")
                self._version_row(target_permit_id, target_version)
                cursor = self.connection.execute(
                    "UPDATE sea_plans SET permit_id=?,permit_version=?,state='draft',revision=revision+1 "
                    "WHERE plan_id=? AND state='manual_review'",
                    (target_permit_id, target_version, plan["plan_id"]),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("方案不在人工处置状态")
                self.connection.execute(
                    "UPDATE plan_segments SET state='pending' WHERE plan_id=? AND state<>'completed'",
                    (plan["plan_id"],),
                )
                new_state = "draft"
                self._audit(
                    "plan",
                    plan["plan_id"],
                    "plan.repinned",
                    actor_id,
                    {
                        "case_id": case_id,
                        "permit_id": target_permit_id,
                        "permit_version": target_version,
                        "note": note,
                    },
                )
            cursor = self.connection.execute(
                "UPDATE manual_cases SET state='resolved',resolution=?,resolved_by=?,resolved_at=? "
                "WHERE case_id=? AND state='open'",
                (f"{action}: {note}", actor_id, resolved_at, case_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("人工处置单已办结")
            self._audit(
                "manual_case",
                str(case_id),
                "manual.resolved",
                actor_id,
                {"action": action, "plan_id": plan["plan_id"], "note": note},
            )
        return {
            "case_id": case_id,
            "state": "resolved",
            "action": action,
            "plan_id": plan["plan_id"],
            "plan_state": new_state,
        }

    # ---------- 角色视图 ----------

    def _segments_with_evaluations(
        self, plan: sqlite3.Row
    ) -> tuple[list[sqlite3.Row], dict[int, sqlite3.Row]]:
        segments = self.connection.execute(
            "SELECT * FROM plan_segments WHERE plan_id=? ORDER BY segment_index", (plan["plan_id"],)
        ).fetchall()
        evaluations = self.connection.execute(
            "SELECT * FROM segment_evaluations WHERE plan_id=? AND permit_id=? AND permit_version=? "
            "ORDER BY segment_index",
            (plan["plan_id"], plan["permit_id"], plan["permit_version"]),
        ).fetchall()
        return segments, {row["segment_index"]: row for row in evaluations}

    @staticmethod
    def _segment_view(
        segment: sqlite3.Row,
        evaluation: sqlite3.Row | None,
        *,
        include_path: bool,
        include_evidence: bool,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "segment_index": segment["segment_index"],
            "state": segment["state"],
            "starts_at": segment["starts_at"],
            "ends_at": segment["ends_at"],
        }
        if include_path:
            item["path"] = json.loads(segment["path_json"])
        if segment["completed_at"] is not None:
            item["completed_at"] = segment["completed_at"]
        if evaluation is None:
            item["verdict"] = None
            item["exclusions"] = []
        else:
            item["verdict"] = evaluation["verdict"]
            item["exclusions"] = json.loads(evaluation["reasons_json"])
            if include_evidence:
                item["input_sha256"] = evaluation["input_sha256"]
                item["evaluated_by"] = evaluation["evaluated_by"]
                item["evaluated_at"] = evaluation["evaluated_at"]
        return item

    def _manual_cases(self, plan_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM manual_cases WHERE plan_id=? ORDER BY case_id", (plan_id,)
        ).fetchall()

    def construction_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "view.construction")
        plan = self._plan(plan_id)
        permit = self._permit(plan["permit_id"])
        segments, evaluations = self._segments_with_evaluations(plan)
        return {
            "view": "construction",
            "plan_id": plan["plan_id"],
            "kind": plan["kind"],
            "title": plan["title"],
            "party_id": plan["party_id"],
            "state": plan["state"],
            "revision": plan["revision"],
            "permit": {
                "permit_id": permit["permit_id"],
                "version": plan["permit_version"],
                "state": permit["state"],
                "responsible_party": permit["responsible_party"],
            },
            "segments": [
                self._segment_view(
                    segment,
                    evaluations.get(segment["segment_index"]),
                    include_path=True,
                    include_evidence=False,
                )
                for segment in segments
            ],
            "manual_cases": [
                {
                    "case_id": case["case_id"],
                    "trigger": case["trigger"],
                    "prior_state": case["prior_state"],
                    "state": case["state"],
                    "resolution": case["resolution"],
                }
                for case in self._manual_cases(plan_id)
            ],
        }

    def om_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "view.om")
        plan = self._plan(plan_id)
        segments, evaluations = self._segments_with_evaluations(plan)
        om_zones = self.connection.execute(
            "SELECT zone_id,name FROM constraint_zones WHERE active=1 AND category='om' ORDER BY zone_id"
        ).fetchall()
        return {
            "view": "om",
            "plan_id": plan["plan_id"],
            "kind": plan["kind"],
            "state": plan["state"],
            "permit_id": plan["permit_id"],
            "permit_version": plan["permit_version"],
            "segments": [
                self._segment_view(
                    segment,
                    evaluations.get(segment["segment_index"]),
                    include_path=True,
                    include_evidence=False,
                )
                for segment in segments
            ],
            "om_boundaries": [{"zone_id": row["zone_id"], "name": row["name"]} for row in om_zones],
        }

    def audit_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "view.audit")
        plan = self._plan(plan_id)
        permit = self._permit(plan["permit_id"])
        segments, evaluations = self._segments_with_evaluations(plan)
        case_ids = [str(case["case_id"]) for case in self._manual_cases(plan_id)]
        events: list[sqlite3.Row] = []
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            events = self.connection.execute(
                f"SELECT * FROM sea_audit_events WHERE (entity_type='plan' AND entity_id=?) "
                f"OR (entity_type='manual_case' AND entity_id IN ({placeholders})) ORDER BY event_id",
                (plan_id, *case_ids),
            ).fetchall()
        else:
            events = self.connection.execute(
                "SELECT * FROM sea_audit_events WHERE entity_type='plan' AND entity_id=? ORDER BY event_id",
                (plan_id,),
            ).fetchall()
        return {
            "view": "audit",
            "plan_id": plan["plan_id"],
            "kind": plan["kind"],
            "title": plan["title"],
            "party_id": plan["party_id"],
            "state": plan["state"],
            "revision": plan["revision"],
            "permit": {
                "permit_id": permit["permit_id"],
                "version": plan["permit_version"],
                "state": permit["state"],
                "responsible_party": permit["responsible_party"],
            },
            "segments": [
                self._segment_view(
                    segment,
                    evaluations.get(segment["segment_index"]),
                    include_path=True,
                    include_evidence=True,
                )
                for segment in segments
            ],
            "events": [
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "actor_id": event["actor_id"],
                    "payload": json.loads(event["payload_json"]),
                    "created_at": event["created_at"],
                }
                for event in events
            ],
        }

    def plan_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        view = PLAN_VIEW_BY_ROLE.get(user["role"])
        if view == "construction":
            return self.construction_view(actor_id, plan_id)
        if view == "om":
            return self.om_view(actor_id, plan_id)
        if view == "audit":
            return self.audit_view(actor_id, plan_id)
        raise Forbidden(f"角色 {user['role']} 没有方案视图")

    def permit_view(self, actor_id: str, permit_id: str) -> dict[str, Any]:
        self._require(actor_id, "permit.read")
        permit = self._permit(permit_id)
        versions = self.connection.execute(
            "SELECT * FROM permit_versions WHERE permit_id=? ORDER BY version", (permit_id,)
        ).fetchall()
        version_items: list[dict[str, Any]] = []
        for version in versions:
            restrictions = self.connection.execute(
                "SELECT window_id,kind,starts_at,ends_at FROM restriction_windows "
                "WHERE permit_id=? AND version=? ORDER BY window_id",
                (permit_id, version["version"]),
            ).fetchall()
            authorizations = self.connection.execute(
                "SELECT authorization_id,party_id,scope,valid_from,valid_until FROM party_authorizations "
                "WHERE permit_id=? AND version=? ORDER BY authorization_id",
                (permit_id, version["version"]),
            ).fetchall()
            commitments = self.connection.execute(
                "SELECT commitment_id,expires_at FROM permit_commitments "
                "WHERE permit_id=? AND version=? ORDER BY commitment_id",
                (permit_id, version["version"]),
            ).fetchall()
            version_items.append(
                {
                    "version": version["version"],
                    "content_sha256": version["content_sha256"],
                    "registered_by": version["registered_by"],
                    "registered_at": version["registered_at"],
                    "restriction_windows": [dict(row) for row in restrictions],
                    "authorizations": [dict(row) for row in authorizations],
                    "commitments": [dict(row) for row in commitments],
                }
            )
        return {
            "permit_id": permit["permit_id"],
            "title": permit["title"],
            "responsible_party": permit["responsible_party"],
            "state": permit["state"],
            "revoke_reason": permit["revoke_reason"],
            "revoked_at": permit["revoked_at"],
            "versions": version_items,
        }

    def constraints_view(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "constraint.read")
        zones = self.connection.execute(
            "SELECT zone_id,category,rule,name,geometry_json,source_batch_id FROM constraint_zones "
            "WHERE active=1 ORDER BY category,zone_id"
        ).fetchall()
        cables = self.connection.execute(
            "SELECT cable_id,name,path_json,protection_distance_m,source_batch_id FROM cable_corridors "
            "WHERE active=1 ORDER BY cable_id"
        ).fetchall()
        return {
            "zones": [
                {
                    "zone_id": row["zone_id"],
                    "category": row["category"],
                    "rule": row["rule"],
                    "name": row["name"],
                    "geometry": json.loads(row["geometry_json"]),
                    "source_batch_id": row["source_batch_id"],
                }
                for row in zones
            ],
            "cables": [
                {
                    "cable_id": row["cable_id"],
                    "name": row["name"],
                    "path": json.loads(row["path_json"]),
                    "protection_distance_m": row["protection_distance_m"],
                    "source_batch_id": row["source_batch_id"],
                }
                for row in cables
            ],
        }

    def import_view(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "permit.read")
        row = self.connection.execute(
            "SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("导入批次不存在")
        return {
            **json.loads(row["response_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM sea_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
