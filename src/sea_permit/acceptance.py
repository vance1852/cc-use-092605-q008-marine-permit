"""贯通许可登记、批量导入、方案评估、撤回与人工处置的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import PermitService


PERMIT_BOUNDARY = [[122.0, 30.0], [122.1, 30.0], [122.1, 30.1], [122.0, 30.1]]
NAVIGATION_ZONE = [[122.05, 30.02], [122.06, 30.02], [122.06, 30.03], [122.05, 30.03]]
ECOLOGY_ZONE = [[122.08, 30.08], [122.09, 30.08], [122.09, 30.09], [122.08, 30.09]]
OM_ZONE = [[121.99, 29.99], [122.11, 29.99], [122.11, 30.11], [121.99, 30.11]]
EXISTING_CABLE = [[122.0, 30.05], [122.1, 30.05]]


def _segment(path: list[list[float]], starts_at: str, ends_at: str) -> dict[str, object]:
    return {"path": {"type": "line", "coordinates": path}, "starts_at": starts_at, "ends_at": ends_at}


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = PermitService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("build", "constructor"),
        ("ops", "operator"),
        ("reg", "regulator"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.create_permit("reg", {"permit_id": "hai-001", "title": "帆石海域使用许可", "responsible_party": "builder-one"})
    service.register_boundary_version("reg", "hai-001", {"boundary": {"type": "polygon", "coordinates": PERMIT_BOUNDARY}})
    service.register_restriction(
        "reg",
        "hai-001",
        {
            "window_id": "win-fishery-001",
            "version": 1,
            "kind": "fishery_closure",
            "starts_at": "2026-10-05T00:00:00Z",
            "ends_at": "2026-10-08T00:00:00Z",
            "note": "渔业协商禁作期",
        },
    )
    service.grant_authorization(
        "reg",
        "hai-001",
        {
            "authorization_id": "auth-001",
            "version": 1,
            "party_id": "builder-one",
            "scope": "both",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2027-03-01T00:00:00Z",
        },
    )
    service.register_commitment(
        "reg",
        "hai-001",
        {
            "commitment_id": "com-001",
            "version": 1,
            "summary": "保持航道与生态边界数据有效",
            "expires_at": "2027-01-01T00:00:00Z",
        },
    )

    batch = {
        "batch_id": "batch-001",
        "items": [
            {"type": "zone", "zone_id": "zone-nav-001", "category": "navigation", "rule": "outside",
             "name": "航道安全区", "geometry": {"type": "polygon", "coordinates": NAVIGATION_ZONE}},
            {"type": "zone", "zone_id": "zone-eco-001", "category": "ecology", "rule": "outside",
             "name": "生态红线区", "geometry": {"type": "polygon", "coordinates": ECOLOGY_ZONE}},
            {"type": "zone", "zone_id": "zone-om-001", "category": "om", "rule": "inside",
             "name": "运维覆盖区", "geometry": {"type": "polygon", "coordinates": OM_ZONE}},
            {"type": "cable", "cable_id": "cable-001", "name": "既有送出海缆",
             "path": {"type": "line", "coordinates": EXISTING_CABLE}, "protection_distance_m": "500"},
        ],
    }
    imported = service.import_constraints("reg", batch)
    replayed = service.import_constraints("reg", batch)

    service.create_plan(
        "build",
        {
            "plan_id": "plan-a",
            "kind": "construction",
            "title": "扩建风机基础施工",
            "permit_id": "hai-001",
            "permit_version": 1,
            "party_id": "builder-one",
            "segments": [
                _segment([[122.02, 30.01], [122.03, 30.01]], "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z"),
                _segment([[122.04, 30.025], [122.07, 30.025]], "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z"),
                _segment([[122.05, 30.051], [122.06, 30.051]], "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z"),
                _segment([[122.02, 30.07], [122.03, 30.07]], "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"),
            ],
            "idempotency_key": "plan-key-a",
        },
    )
    evaluation_a = service.evaluate_plan("build", "plan-a")

    service.create_plan(
        "build",
        {
            "plan_id": "plan-b",
            "kind": "export",
            "title": "新增送出海缆敷设",
            "permit_id": "hai-001",
            "permit_version": 1,
            "party_id": "builder-one",
            "segments": [
                _segment([[122.02, 30.01], [122.03, 30.01]], "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z"),
                _segment([[122.07, 30.06], [122.075, 30.07]], "2026-10-12T00:00:00Z", "2026-10-14T00:00:00Z"),
            ],
            "idempotency_key": "plan-key-b",
        },
    )
    service.evaluate_plan("build", "plan-b")
    service.confirm_plan("build", "plan-b", 1)
    service.start_plan("ops", "plan-b", 2)
    service.complete_segment("ops", "plan-b", 0)
    service.complete_segment("ops", "plan-b", 1)
    service.complete_plan("ops", "plan-b", 3)

    service.create_plan(
        "build",
        {
            "plan_id": "plan-c",
            "kind": "construction",
            "title": "二期机组安装",
            "permit_id": "hai-001",
            "permit_version": 1,
            "party_id": "builder-one",
            "segments": [
                _segment([[122.02, 30.02], [122.03, 30.02]], "2026-10-10T00:00:00Z", "2026-10-13T00:00:00Z"),
                _segment([[122.03, 30.06], [122.04, 30.06]], "2026-10-11T00:00:00Z", "2026-10-14T00:00:00Z"),
            ],
            "idempotency_key": "plan-key-c",
        },
    )
    service.evaluate_plan("build", "plan-c")
    service.confirm_plan("build", "plan-c", 1)
    service.start_plan("ops", "plan-c", 2)
    service.complete_segment("ops", "plan-c", 0)

    service.create_plan(
        "build",
        {
            "plan_id": "plan-d",
            "kind": "construction",
            "title": "三期预留机位",
            "permit_id": "hai-001",
            "permit_version": 1,
            "party_id": "builder-one",
            "segments": [
                _segment([[122.02, 30.03], [122.03, 30.03]], "2026-10-10T00:00:00Z", "2026-10-12T00:00:00Z"),
            ],
            "idempotency_key": "plan-key-d",
        },
    )

    revocation = service.revoke_permit("reg", "hai-001", "海域使用许可被主管部门撤回")
    resolution = service.resolve_manual_case(
        "reg",
        revocation["manual_case_ids"][0],
        {"action": "halt", "note": "许可撤回，停止后续施工，保留已完成线段"},
    )

    result = {
        "status": "ok",
        "import": imported,
        "import_replayed": replayed["replayed"],
        "evaluation_a": {
            "candidates": evaluation_a["candidates"],
            "excluded_codes": {
                item["segment_index"]: [reason["code"] for reason in item["reasons"]]
                for item in evaluation_a["excluded"]
            },
        },
        "revocation": revocation,
        "resolution": resolution,
        "construction_view": service.construction_view("build", "plan-c"),
        "om_view": service.om_view("ops", "plan-c"),
        "audit_view_events": len(service.audit_view("audit", "plan-c")["events"]),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行海域许可联动服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
