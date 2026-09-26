"""海域许可联动的事务用例:边界版本、限制时段、授权、方案筛查与撤回级联。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    AuthorizationInput,
    BatchInput,
    BoundaryVersionInput,
    PlanInput,
    RestrictionWindowInput,
    boundary_refs,
)
from .screening import REQUIRED_PLAN_KINDS, canonical_json, decimal_text, digest, evaluate_segments
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "permit_admin": {
        "boundary.write", "boundary.withdraw", "window.write",
        "authorization.write", "batch.import", "boundary.read", "plan.read",
    },
    "construction": {
        "plan.write", "plan.screen", "plan.confirm", "plan.start",
        "plan.read", "boundary.read",
    },
    "operations": {"plan.read", "boundary.read", "disposition.write"},
    "auditor": {"plan.read", "boundary.read", "audit.read"},
}

UNCONFIRMED_STATES = ("draft", "screened", "confirmed")
PLAN_STATES = (
    "draft", "screened", "confirmed", "in_progress",
    "completed", "blocked", "manual_review", "terminated",
)


class SeaPermitService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM permit_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM permit_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO permit_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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
                    "INSERT INTO permit_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 边界版本、限制时段与责任主体授权
    # ------------------------------------------------------------------

    def _boundary_row(self, version_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM boundary_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"边界版本不存在: {version_id}")
        return row

    def _insert_boundary(self, actor_id: str, item: BoundaryVersionInput) -> dict[str, Any]:
        previous = self.connection.execute(
            "SELECT version_id,revision FROM boundary_versions WHERE boundary_name=? AND kind=? "
            "ORDER BY revision DESC LIMIT 1",
            (item.boundary_name, item.kind),
        ).fetchone()
        revision = 1 if previous is None else int(previous["revision"]) + 1
        supersedes = None if previous is None else previous["version_id"]
        content = {
            "boundary_name": item.boundary_name,
            "kind": item.kind,
            "geometry": item.geometry,
            "buffer_m": decimal_text(item.buffer_m),
        }
        content_sha256 = digest(content)
        self.connection.execute(
            "INSERT INTO boundary_versions(version_id,boundary_name,kind,geometry_json,buffer_m,revision,"
            "supersedes_version_id,content_sha256,registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                item.version_id,
                item.boundary_name,
                item.kind,
                canonical_json(item.geometry),
                decimal_text(item.buffer_m),
                revision,
                supersedes,
                content_sha256,
                actor_id,
                self._now(),
            ),
        )
        return {
            "version_id": item.version_id,
            "boundary_name": item.boundary_name,
            "kind": item.kind,
            "revision": revision,
            "supersedes_version_id": supersedes,
            "content_sha256": content_sha256,
            "state": "active",
        }

    def register_boundary(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "boundary.write")
        item = BoundaryVersionInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                result = self._insert_boundary(actor_id, item)
                self._audit("boundary", item.version_id, "boundary.registered", actor_id, result)
        except sqlite3.IntegrityError as exc:
            raise Conflict("边界版本编号已经存在") from exc
        return result

    def _boundary_view(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["geometry"] = json.loads(result.pop("geometry_json"))
        return result

    def list_boundaries(self, actor_id: str, kind: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "boundary.read")
        if kind is None:
            rows = self.connection.execute(
                "SELECT * FROM boundary_versions ORDER BY boundary_name,kind,revision"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM boundary_versions WHERE kind=? ORDER BY boundary_name,revision",
                (kind,),
            ).fetchall()
        return {"boundaries": [self._boundary_view(row) for row in rows]}

    def boundary_detail(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._require(actor_id, "boundary.read")
        row = self._boundary_row(version_id)
        windows = self.connection.execute(
            "SELECT * FROM restriction_windows WHERE boundary_version_id=? ORDER BY starts_at,window_id",
            (version_id,),
        ).fetchall()
        authorizations = self.connection.execute(
            "SELECT * FROM authorizations WHERE boundary_version_id=? ORDER BY authorization_id",
            (version_id,),
        ).fetchall()
        return {
            **self._boundary_view(row),
            "restriction_windows": [dict(item) for item in windows],
            "authorizations": [dict(item) for item in authorizations],
        }

    def _insert_window(self, actor_id: str, item: RestrictionWindowInput) -> None:
        self.connection.execute(
            "INSERT INTO restriction_windows(window_id,boundary_version_id,label,starts_at,ends_at,"
            "commitment_expires_at,registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                item.window_id,
                item.boundary_version_id,
                item.label,
                item.starts_at,
                item.ends_at,
                item.commitment_expires_at,
                actor_id,
                self._now(),
            ),
        )

    def register_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "window.write")
        item = RestrictionWindowInput.from_dict(raw)
        boundary = self._boundary_row(item.boundary_version_id)
        if boundary["state"] != "active":
            raise InvalidState("不能在已撤回版本上登记限制时段")
        try:
            with transaction(self.connection, immediate=True):
                self._insert_window(actor_id, item)
                self._audit("window", item.window_id, "window.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("限制时段编号已经存在") from exc
        return {"window_id": item.window_id, "boundary_version_id": item.boundary_version_id, "state": "active"}

    def _insert_authorization(self, actor_id: str, item: AuthorizationInput) -> None:
        self.connection.execute(
            "INSERT INTO authorizations(authorization_id,boundary_version_id,grantor_party,grantee_party,"
            "commitment_expires_at,registered_by,registered_at) VALUES(?,?,?,?,?,?,?)",
            (
                item.authorization_id,
                item.boundary_version_id,
                item.grantor_party,
                item.grantee_party,
                item.commitment_expires_at,
                actor_id,
                self._now(),
            ),
        )

    def register_authorization(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "authorization.write")
        item = AuthorizationInput.from_dict(raw)
        boundary = self._boundary_row(item.boundary_version_id)
        if boundary["state"] != "active":
            raise InvalidState("不能在已撤回版本上登记授权")
        try:
            with transaction(self.connection, immediate=True):
                self._insert_authorization(actor_id, item)
                self._audit("authorization", item.authorization_id, "authorization.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号已经存在") from exc
        return {
            "authorization_id": item.authorization_id,
            "boundary_version_id": item.boundary_version_id,
            "grantee_party": item.grantee_party,
            "state": "active",
        }

    # ------------------------------------------------------------------
    # 许可撤回级联
    # ------------------------------------------------------------------

    def _cascade_permit_loss(
        self,
        actor_id: str,
        *,
        trigger: Mapping[str, Any],
        matches,
    ) -> dict[str, list[str]]:
        """许可失效级联:未确认方案立即阻止,执行中项目转人工处置。

        只改变方案状态并追加审计事件,不改写方案引用的版本(不做静默迁移),
        已完成方案不受影响,已发生的合法施工记录保持完整。
        """
        affected: dict[str, list[str]] = {"blocked": [], "manual_review": []}
        rows = self.connection.execute(
            "SELECT plan_id,party,state,boundary_refs_json FROM permit_plans "
            "WHERE state IN ('draft','screened','confirmed','in_progress') ORDER BY plan_id"
        ).fetchall()
        for plan in rows:
            refs = json.loads(plan["boundary_refs_json"])
            if not matches(plan, refs):
                continue
            if plan["state"] in UNCONFIRMED_STATES:
                new_state, event_type = "blocked", "plan.blocked"
            else:
                new_state, event_type = "manual_review", "plan.manual_review"
            self.connection.execute(
                "UPDATE permit_plans SET state=?,revision=revision+1,updated_at=? WHERE plan_id=?",
                (new_state, self._now(), plan["plan_id"]),
            )
            self._audit("plan", plan["plan_id"], event_type, actor_id, dict(trigger))
            affected[new_state].append(plan["plan_id"])
        return affected

    def withdraw_boundary(self, actor_id: str, version_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "boundary.withdraw")
        row = self._boundary_row(version_id)
        if row["state"] != "active":
            raise InvalidState("边界版本已撤回")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        reason_text = reason.strip()
        trigger = {"trigger": "boundary.withdrawn", "version_id": version_id, "reason": reason_text}
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE boundary_versions SET state='withdrawn',withdrawn_at=?,withdrawn_by=?,"
                "withdraw_reason=? WHERE version_id=?",
                (self._now(), actor_id, reason_text, version_id),
            )
            affected = self._cascade_permit_loss(
                actor_id,
                trigger=trigger,
                matches=lambda plan, refs: version_id in refs.values(),
            )
            self._audit(
                "boundary", version_id, "boundary.withdrawn", actor_id,
                {"reason": reason_text, "affected": affected},
            )
        return {"version_id": version_id, "state": "withdrawn", "affected": affected}

    def revoke_authorization(self, actor_id: str, authorization_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "authorization.write")
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFound("授权不存在")
        if row["state"] != "active":
            raise InvalidState("授权已撤销")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        reason_text = reason.strip()
        trigger = {
            "trigger": "authorization.revoked",
            "authorization_id": authorization_id,
            "reason": reason_text,
        }
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE authorizations SET state='revoked',revoked_at=?,revoked_by=? WHERE authorization_id=?",
                (self._now(), actor_id, authorization_id),
            )
            affected = self._cascade_permit_loss(
                actor_id,
                trigger=trigger,
                matches=lambda plan, refs: (
                    row["boundary_version_id"] in refs.values()
                    and plan["party"] == row["grantee_party"]
                ),
            )
            self._audit(
                "authorization", authorization_id, "authorization.revoked", actor_id,
                {"reason": reason_text, "affected": affected},
            )
        return {"authorization_id": authorization_id, "state": "revoked", "affected": affected}

    # ------------------------------------------------------------------
    # 批量导入:全有或全无,重复提交返回稳定结果
    # ------------------------------------------------------------------

    def _exists(self, table: str, key: str, value: str) -> bool:
        row = self.connection.execute(
            f"SELECT 1 FROM {table} WHERE {key}=?", (value,)
        ).fetchone()
        return row is not None

    def import_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.import")
        batch = BatchInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM permit_idempotency WHERE scope='batch' AND idempotency_key=?",
            (batch.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同批量内容")
            return json.loads(stored["response_json"])
        known_versions = {item.version_id for item in batch.boundaries}
        for item in (*batch.windows, *batch.authorizations):
            target = item.boundary_version_id
            if target not in known_versions and not self._exists("boundary_versions", "version_id", target):
                raise ValidationFailed(f"引用的边界版本不存在: {target}")
        for item in batch.boundaries:
            if self._exists("boundary_versions", "version_id", item.version_id):
                raise Conflict(f"边界版本编号已经存在: {item.version_id}")
        for item in batch.windows:
            if self._exists("restriction_windows", "window_id", item.window_id):
                raise Conflict(f"限制时段编号已经存在: {item.window_id}")
        for item in batch.authorizations:
            if self._exists("authorizations", "authorization_id", item.authorization_id):
                raise Conflict(f"授权编号已经存在: {item.authorization_id}")
        response = {
            "batch_id": batch.batch_id,
            "imported": {
                "boundaries": len(batch.boundaries),
                "windows": len(batch.windows),
                "authorizations": len(batch.authorizations),
            },
            "version_ids": [item.version_id for item in batch.boundaries],
            "window_ids": [item.window_id for item in batch.windows],
            "authorization_ids": [item.authorization_id for item in batch.authorizations],
        }
        with transaction(self.connection, immediate=True):
            for item in batch.boundaries:
                self._insert_boundary(actor_id, item)
            for item in batch.windows:
                self._insert_window(actor_id, item)
            for item in batch.authorizations:
                self._insert_authorization(actor_id, item)
            self.connection.execute(
                "INSERT INTO permit_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('batch',?,?,?,?)",
                (batch.idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit("batch", batch.batch_id, "batch.imported", actor_id, response["imported"])
        return response

    # ------------------------------------------------------------------
    # 施工与送出方案
    # ------------------------------------------------------------------

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM permit_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def _validate_refs(self, refs: Mapping[str, str]) -> None:
        for kind in REQUIRED_PLAN_KINDS:
            row = self.connection.execute(
                "SELECT kind,state FROM boundary_versions WHERE version_id=?", (refs[kind],)
            ).fetchone()
            if row is None:
                raise NotFound(f"边界版本不存在: {refs[kind]}")
            if row["kind"] != kind:
                raise ValidationFailed(f"版本 {refs[kind]} 的类别不是 {kind}")
            if row["state"] != "active":
                raise InvalidState(f"边界版本已撤回: {refs[kind]}")

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = PlanInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM permit_idempotency WHERE scope='plan' AND idempotency_key=?",
            (plan.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同方案内容")
            return json.loads(stored["response_json"])
        self._validate_refs(plan.boundary_refs)
        response = {"plan_id": plan.plan_id, "kind": plan.kind, "state": "draft", "revision": 1}
        segments = [
            {"segment_id": item.segment_id, "points": item.points} for item in plan.segments
        ]
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO permit_plans(plan_id,kind,party,title,segments_json,boundary_refs_json,"
                    "planned_starts_at,planned_ends_at,idempotency_key,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id,
                        plan.kind,
                        plan.party,
                        plan.title,
                        canonical_json(segments),
                        canonical_json(plan.boundary_refs),
                        plan.planned_starts_at,
                        plan.planned_ends_at,
                        plan.idempotency_key,
                        actor_id,
                        self._now(),
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO permit_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('plan',?,?,?,?)",
                    (plan.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("plan", plan.plan_id, "plan.created", actor_id, {"kind": plan.kind, "party": plan.party})
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号或幂等键冲突") from exc
        return response

    def _version_payload(self, version_id: str) -> dict[str, Any]:
        row = self._boundary_row(version_id)
        return {
            "version_id": row["version_id"],
            "boundary_name": row["boundary_name"],
            "kind": row["kind"],
            "geometry": [tuple(point) for point in json.loads(row["geometry_json"])],
            "buffer_m": row["buffer_m"],
            "content_sha256": row["content_sha256"],
        }

    def _active_windows(self, version_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not version_ids:
            return []
        placeholders = ",".join("?" for _ in version_ids)
        rows = self.connection.execute(
            "SELECT w.*,b.geometry_json FROM restriction_windows w "
            "JOIN boundary_versions b ON b.version_id=w.boundary_version_id "
            f"WHERE w.state='active' AND w.boundary_version_id IN ({placeholders}) "
            "ORDER BY w.window_id",
            tuple(version_ids),
        ).fetchall()
        return [
            {
                "window_id": row["window_id"],
                "boundary_version_id": row["boundary_version_id"],
                "label": row["label"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "commitment_expires_at": row["commitment_expires_at"],
                "geometry": [tuple(point) for point in json.loads(row["geometry_json"])],
            }
            for row in rows
        ]

    def _active_authorizations(self, version_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not version_ids:
            return []
        placeholders = ",".join("?" for _ in version_ids)
        rows = self.connection.execute(
            "SELECT * FROM authorizations "
            f"WHERE state='active' AND boundary_version_id IN ({placeholders}) "
            "ORDER BY authorization_id",
            tuple(version_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def screen_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.screen")
        plan = self._plan_row(plan_id)
        if plan["state"] not in ("draft", "screened"):
            raise InvalidState("当前状态不能重新筛查")
        refs = json.loads(plan["boundary_refs_json"])
        self._validate_refs(refs)
        versions = {kind: self._version_payload(refs[kind]) for kind in REQUIRED_PLAN_KINDS}
        version_ids = [refs[kind] for kind in REQUIRED_PLAN_KINDS]
        windows = self._active_windows(version_ids)
        authorizations = self._active_authorizations(version_ids)
        segments = json.loads(plan["segments_json"])
        input_value = {
            "plan_id": plan_id,
            "plan_revision": plan["revision"],
            "party": plan["party"],
            "planned_starts_at": plan["planned_starts_at"],
            "planned_ends_at": plan["planned_ends_at"],
            "segments": segments,
            "boundary_refs": {
                kind: {"version_id": versions[kind]["version_id"], "content_sha256": versions[kind]["content_sha256"]}
                for kind in REQUIRED_PLAN_KINDS
            },
            "windows": windows,
            "authorizations": authorizations,
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT screening_id,result_json FROM plan_screenings "
            "WHERE plan_id=? AND plan_revision=? AND input_sha256=?",
            (plan_id, plan["revision"], input_sha256),
        ).fetchone()
        if existing is not None:
            return {
                "screening_id": existing["screening_id"],
                "plan_id": plan_id,
                **json.loads(existing["result_json"]),
                "replayed": True,
            }
        result = evaluate_segments(
            segments=segments,
            versions=versions,
            windows=windows,
            authorizations=authorizations,
            party=plan["party"],
            planned_starts_at=plan["planned_starts_at"],
            planned_ends_at=plan["planned_ends_at"],
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO plan_screenings(plan_id,plan_revision,input_sha256,admissible,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    plan_id,
                    plan["revision"],
                    input_sha256,
                    1 if result["admissible"] else 0,
                    canonical_json(result),
                    actor_id,
                    self._now(),
                ),
            )
            if plan["state"] == "draft":
                self.connection.execute(
                    "UPDATE permit_plans SET state='screened',updated_at=? WHERE plan_id=?",
                    (self._now(), plan_id),
                )
            screening_id = int(cursor.lastrowid)
            self._audit(
                "plan", plan_id, "plan.screened", actor_id,
                {"screening_id": screening_id, "admissible": result["admissible"]},
            )
        return {"screening_id": screening_id, "plan_id": plan_id, **result, "replayed": False}

    def _require_refs_current(self, plan: sqlite3.Row) -> None:
        refs = json.loads(plan["boundary_refs_json"])
        planned_end = parse_utc(plan["planned_ends_at"], "planned_ends_at")
        for kind in REQUIRED_PLAN_KINDS:
            row = self.connection.execute(
                "SELECT state FROM boundary_versions WHERE version_id=?", (refs[kind],)
            ).fetchone()
            if row is None or row["state"] != "active":
                raise Conflict(f"引用版本已撤回: {refs[kind]}")
            covering = self.connection.execute(
                "SELECT commitment_expires_at FROM authorizations "
                "WHERE boundary_version_id=? AND grantee_party=? AND state='active'",
                (refs[kind], plan["party"]),
            ).fetchall()
            if not any(
                parse_utc(item["commitment_expires_at"], "commitment_expires_at") >= planned_end
                for item in covering
            ):
                raise Conflict(f"责任主体授权缺失或承诺已到期: {refs[kind]}")

    def _latest_screening(self, plan_id: str, revision: int | None = None) -> sqlite3.Row | None:
        if revision is None:
            return self.connection.execute(
                "SELECT * FROM plan_screenings WHERE plan_id=? ORDER BY screening_id DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM plan_screenings WHERE plan_id=? AND plan_revision=? "
            "ORDER BY screening_id DESC LIMIT 1",
            (plan_id, revision),
        ).fetchone()

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan = self._plan_row(plan_id)
        if plan["state"] != "screened" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前已筛查版本")
        screening = self._latest_screening(plan_id, plan["revision"])
        if screening is None or not screening["admissible"]:
            raise InvalidState("方案存在排除依据,不能确认")
        self._require_refs_current(plan)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE permit_plans SET state='confirmed',revision=revision+1,updated_at=? "
                "WHERE plan_id=? AND state='screened' AND revision=?",
                (self._now(), plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前已筛查版本")
            self._audit("plan", plan_id, "plan.confirmed", actor_id, {"screening_id": screening["screening_id"]})
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    def start_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.start")
        plan = self._plan_row(plan_id)
        if plan["state"] != "confirmed" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前已确认版本")
        self._require_refs_current(plan)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE permit_plans SET state='in_progress',revision=revision+1,updated_at=? "
                "WHERE plan_id=? AND state='confirmed' AND revision=?",
                (self._now(), plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不是当前已确认版本")
            self._audit("plan", plan_id, "plan.started", actor_id, {})
        return {"plan_id": plan_id, "state": "in_progress", "revision": expected_revision + 1}

    def complete_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan = self._plan_row(plan_id)
        if plan["state"] != "in_progress" or plan["revision"] != expected_revision:
            raise InvalidState("方案不在执行中")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE permit_plans SET state='completed',revision=revision+1,updated_at=? "
                "WHERE plan_id=? AND state='in_progress' AND revision=?",
                (self._now(), plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("方案不在执行中")
            self._audit("plan", plan_id, "plan.completed", actor_id, {})
        return {"plan_id": plan_id, "state": "completed", "revision": expected_revision + 1}

    def resolve_plan(
        self,
        actor_id: str,
        plan_id: str,
        action: str,
        note: str,
        new_boundary_refs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        if plan["state"] == "blocked":
            self._require(actor_id, "plan.write")
        elif plan["state"] == "manual_review":
            self._require(actor_id, "disposition.write")
        else:
            raise InvalidState("只有被阻止或人工处置中的方案可以处置")
        if action not in {"repin", "terminate"}:
            raise ValidationFailed("action 必须是 repin 或 terminate")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("处置说明不能为空")
        note_text = note.strip()
        if action == "terminate":
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE permit_plans SET state='terminated',revision=revision+1,updated_at=? WHERE plan_id=?",
                    (self._now(), plan_id),
                )
                self.connection.execute(
                    "INSERT INTO plan_dispositions(plan_id,action,note,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (plan_id, "terminate", note_text, actor_id, self._now()),
                )
                self._audit("plan", plan_id, "plan.terminated", actor_id, {"note": note_text})
            return {"plan_id": plan_id, "state": "terminated", "revision": plan["revision"] + 1}
        if new_boundary_refs is None:
            raise ValidationFailed("repin 必须提供新的 boundary_refs")
        refs = boundary_refs(new_boundary_refs)
        self._validate_refs(refs)
        previous_refs = json.loads(plan["boundary_refs_json"])
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE permit_plans SET boundary_refs_json=?,state='draft',revision=revision+1,updated_at=? "
                "WHERE plan_id=?",
                (canonical_json(refs), self._now(), plan_id),
            )
            self.connection.execute(
                "INSERT INTO plan_dispositions(plan_id,action,note,actor_id,created_at) VALUES(?,?,?,?,?)",
                (plan_id, "repin", note_text, actor_id, self._now()),
            )
            self._audit(
                "plan", plan_id, "plan.repinned", actor_id,
                {"previous_refs": previous_refs, "new_refs": refs, "note": note_text},
            )
        return {"plan_id": plan_id, "state": "draft", "revision": plan["revision"] + 1, "boundary_refs": refs}

    # ------------------------------------------------------------------
    # 角色视图
    # ------------------------------------------------------------------

    def plan_view(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "plan.read")
        plan = self._plan_row(plan_id)
        refs = json.loads(plan["boundary_refs_json"])
        base = {
            "plan_id": plan["plan_id"],
            "kind": plan["kind"],
            "party": plan["party"],
            "title": plan["title"],
            "state": plan["state"],
            "revision": plan["revision"],
            "planned_starts_at": plan["planned_starts_at"],
            "planned_ends_at": plan["planned_ends_at"],
            "boundary_refs": refs,
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
        }
        if user["role"] == "permit_admin":
            return base
        screening = self._latest_screening(plan_id)
        view = {
            **base,
            "segments": json.loads(plan["segments_json"]),
            "latest_screening": None
            if screening is None
            else {
                "screening_id": screening["screening_id"],
                "plan_revision": screening["plan_revision"],
                "admissible": bool(screening["admissible"]),
                "created_at": screening["created_at"],
                **json.loads(screening["result_json"]),
            },
        }
        if user["role"] == "operations":
            view["restriction_windows"] = [
                {key: value for key, value in window.items() if key != "geometry"}
                for window in self._active_windows(list(refs.values()))
            ]
        if user["role"] == "auditor":
            view["evidence"] = {
                "boundary_content": {
                    kind: {
                        "version_id": refs[kind],
                        "content_sha256": self._boundary_row(refs[kind])["content_sha256"],
                    }
                    for kind in REQUIRED_PLAN_KINDS
                },
                "screening_input_sha256": None if screening is None else screening["input_sha256"],
            }
        return view

    def list_plans(self, actor_id: str, state: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        if state is not None and state not in PLAN_STATES:
            raise ValidationFailed("未知方案状态")
        if state is None:
            rows = self.connection.execute(
                "SELECT plan_id,kind,party,title,state,revision,updated_at FROM permit_plans ORDER BY plan_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT plan_id,kind,party,title,state,revision,updated_at FROM permit_plans "
                "WHERE state=? ORDER BY plan_id",
                (state,),
            ).fetchall()
        return {"plans": [dict(row) for row in rows]}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM permit_audit_events ORDER BY event_id").fetchall()
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
