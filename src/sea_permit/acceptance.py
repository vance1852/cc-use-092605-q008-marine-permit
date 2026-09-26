"""贯通边界版本、限制时段、授权、方案筛查与撤回级联的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SeaPermitService


SEA_AREA = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]
NAVIGATION = [[400, 0], [600, 0], [600, 1000], [400, 1000]]
NAVIGATION_SHIFTED = [[700, 0], [900, 0], [900, 1000], [700, 1000]]
ECOLOGY = [[800, 800], [1000, 800], [1000, 1000], [800, 1000]]
MAINTENANCE = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]
EXISTING_CABLE = [[0, 290], [1000, 290], [1000, 310], [0, 310]]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SeaPermitService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("permit", "permit_admin"),
        ("builder", "construction"),
        ("keeper", "operations"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.register_boundary("permit", {"version_id": "sea-v1", "boundary_name": "北部海域", "kind": "sea_area", "geometry": SEA_AREA})
    service.register_boundary("permit", {"version_id": "nav-v1", "boundary_name": "中部航道", "kind": "navigation", "geometry": NAVIGATION})
    service.register_boundary("permit", {"version_id": "om-v1", "boundary_name": "运维可达区", "kind": "maintenance", "geometry": MAINTENANCE})
    batch = service.import_batch("permit", {
        "batch_id": "batch-foundation",
        "idempotency_key": "batch-key-001",
        "boundaries": [
            {"version_id": "eco-v1", "boundary_name": "礁盘生态区", "kind": "ecology", "geometry": ECOLOGY},
            {"version_id": "cable-v1", "boundary_name": "既有送出电缆", "kind": "cable", "geometry": EXISTING_CABLE, "buffer_m": "30"},
        ],
    })
    replayed_batch = service.import_batch("permit", {
        "batch_id": "batch-foundation",
        "idempotency_key": "batch-key-001",
        "boundaries": [
            {"version_id": "eco-v1", "boundary_name": "礁盘生态区", "kind": "ecology", "geometry": ECOLOGY},
            {"version_id": "cable-v1", "boundary_name": "既有送出电缆", "kind": "cable", "geometry": EXISTING_CABLE, "buffer_m": "30"},
        ],
    })
    service.register_window("permit", {
        "window_id": "fw-2026-autumn",
        "boundary_version_id": "sea-v1",
        "label": "渔业协商禁作期",
        "starts_at": "2026-10-01T00:00:00Z",
        "ends_at": "2026-11-30T23:59:59Z",
        "commitment_expires_at": "2027-12-31T00:00:00Z",
    })
    refs = {"sea_area": "sea-v1", "navigation": "nav-v1", "ecology": "eco-v1", "maintenance": "om-v1", "cable": "cable-v1"}
    for kind, version_id in refs.items():
        service.register_authorization("permit", {
            "authorization_id": f"auth-{kind}",
            "boundary_version_id": version_id,
            "grantor_party": "海域使用权人",
            "grantee_party": "epc-north",
            "commitment_expires_at": "2027-12-31T00:00:00Z",
        })

    service.create_plan("builder", {
        "plan_id": "plan-export-a",
        "kind": "export",
        "party": "epc-north",
        "title": "送出方案甲:穿越航道",
        "segments": [
            {"segment_id": "seg-east", "points": [[100, 100], [300, 100]]},
            {"segment_id": "seg-cross", "points": [[100, 500], [900, 500]]},
        ],
        "boundary_refs": refs,
        "planned_starts_at": "2026-10-15T00:00:00Z",
        "planned_ends_at": "2027-03-01T00:00:00Z",
        "idempotency_key": "plan-key-a",
    })
    screening_a = service.screen_plan("builder", "plan-export-a")

    service.create_plan("builder", {
        "plan_id": "plan-build-b",
        "kind": "construction",
        "party": "epc-north",
        "title": "施工方案乙:西侧风机基础",
        "segments": [{"segment_id": "seg-1", "points": [[100, 100], [200, 150]]}],
        "boundary_refs": refs,
        "planned_starts_at": "2027-01-15T00:00:00Z",
        "planned_ends_at": "2027-06-01T00:00:00Z",
        "idempotency_key": "plan-key-b",
    })
    screening_b = service.screen_plan("builder", "plan-build-b")
    service.confirm_plan("builder", "plan-build-b", 1)
    service.start_plan("builder", "plan-build-b", 2)

    withdrawal = service.withdraw_boundary("permit", "nav-v1", "航道规划调整,原安全区作废")
    service.register_boundary("permit", {"version_id": "nav-v2", "boundary_name": "中部航道", "kind": "navigation", "geometry": NAVIGATION_SHIFTED})
    service.register_authorization("permit", {
        "authorization_id": "auth-navigation-2",
        "boundary_version_id": "nav-v2",
        "grantor_party": "海事管理机构",
        "grantee_party": "epc-north",
        "commitment_expires_at": "2027-12-31T00:00:00Z",
    })
    resolved = service.resolve_plan(
        "keeper", "plan-build-b", "repin", "人工处置:改用新航道版本并重新授权",
        {"sea_area": "sea-v1", "navigation": "nav-v2", "ecology": "eco-v1", "maintenance": "om-v1", "cable": "cable-v1"},
    )
    screening_b2 = service.screen_plan("builder", "plan-build-b")
    service.resolve_plan("builder", "plan-export-a", "terminate", "航道版本撤回,送出方案甲放弃")

    result = {
        "status": "ok",
        "batch": batch,
        "batch_replay_stable": replayed_batch == batch,
        "screening_a_admissible": screening_a["admissible"],
        "screening_a_excluded_segments": [
            row["segment_id"] for row in screening_a["segments"] if row["status"] == "excluded"
        ],
        "screening_b_admissible": screening_b["admissible"],
        "withdrawal": withdrawal,
        "resolved": resolved,
        "screening_b2_admissible": screening_b2["admissible"],
        "manual_review_queue": service.list_plans("keeper", "manual_review"),
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
