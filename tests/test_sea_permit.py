from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sea_permit.acceptance import run as acceptance_run
from sea_permit.api import JsonApplication
from sea_permit.clock import FrozenClock
from sea_permit.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from sea_permit.geometry import (
    point_in_polygon,
    polyline_inside_polygon,
    polyline_intersects_polygon,
    polyline_polygon_distance,
    segments_intersect,
)
from sea_permit.screening import evaluate_segments
from sea_permit.service import SeaPermitService


ROOT = Path(__file__).resolve().parents[1]

SEA = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]
NAV = [[400, 0], [600, 0], [600, 1000], [400, 1000]]
ECO = [[800, 800], [1000, 800], [1000, 1000], [800, 1000]]
OM = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]
CABLE = [[0, 290], [1000, 290], [1000, 310], [0, 310]]

REFS = {"sea_area": "sea-v1", "navigation": "nav-v1", "ecology": "eco-v1", "maintenance": "om-v1", "cable": "cable-v1"}


def versions_payload() -> dict[str, dict[str, object]]:
    return {
        "sea_area": {"version_id": "sea-v1", "boundary_name": "北部海域", "geometry": SEA, "buffer_m": "0"},
        "navigation": {"version_id": "nav-v1", "boundary_name": "中部航道", "geometry": NAV, "buffer_m": "0"},
        "ecology": {"version_id": "eco-v1", "boundary_name": "礁盘生态区", "geometry": ECO, "buffer_m": "0"},
        "maintenance": {"version_id": "om-v1", "boundary_name": "运维可达区", "geometry": OM, "buffer_m": "0"},
        "cable": {"version_id": "cable-v1", "boundary_name": "既有送出电缆", "geometry": CABLE, "buffer_m": "30"},
    }


def authorizations_for(party: str, commitment: str = "2027-12-31T00:00:00Z") -> list[dict[str, str]]:
    return [
        {
            "authorization_id": f"auth-{kind}",
            "boundary_version_id": version_id,
            "grantee_party": party,
            "commitment_expires_at": commitment,
        }
        for kind, version_id in REFS.items()
    ]


class GeometryTests(unittest.TestCase):
    def test_point_in_polygon_counts_boundary_as_inside(self) -> None:
        self.assertTrue(point_in_polygon((500, 500), SEA))
        self.assertTrue(point_in_polygon((0, 500), SEA))
        self.assertFalse(point_in_polygon((1500, 500), SEA))

    def test_segments_intersect_covers_crossing_touching_and_disjoint(self) -> None:
        self.assertTrue(segments_intersect((0, 0), (10, 10), (0, 10), (10, 0)))
        self.assertTrue(segments_intersect((0, 0), (10, 0), (5, 0), (15, 0)))
        self.assertFalse(segments_intersect((0, 0), (1, 0), (0, 5), (1, 5)))

    def test_polyline_inside_polygon_allows_hugging_boundary(self) -> None:
        self.assertTrue(polyline_inside_polygon([(0, 0), (1000, 0)], SEA))
        self.assertTrue(polyline_inside_polygon([(100, 100), (200, 200)], SEA))
        self.assertFalse(polyline_inside_polygon([(100, 100), (2000, 2000)], SEA))
        self.assertFalse(polyline_inside_polygon([(-100, 100), (100, 100)], SEA))

    def test_polyline_intersects_polygon_detects_crossing(self) -> None:
        self.assertTrue(polyline_intersects_polygon([(100, 500), (900, 500)], NAV))
        self.assertFalse(polyline_intersects_polygon([(100, 100), (200, 150)], NAV))

    def test_polyline_polygon_distance_is_zero_on_contact(self) -> None:
        self.assertEqual(polyline_polygon_distance([(100, 250), (200, 250)], CABLE), 40.0)
        self.assertEqual(polyline_polygon_distance([(100, 285), (200, 285)], CABLE), 5.0)
        self.assertEqual(polyline_polygon_distance([(100, 300), (200, 300)], CABLE), 0.0)


class ScreeningTests(unittest.TestCase):
    def evaluate(self, segments, **overrides):
        params = {
            "segments": segments,
            "versions": versions_payload(),
            "windows": [],
            "authorizations": authorizations_for("epc-north"),
            "party": "epc-north",
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
        }
        params.update(overrides)
        return evaluate_segments(**params)

    def test_compliant_segment_is_admissible(self) -> None:
        result = self.evaluate([{"segment_id": "s1", "points": [[100, 100], [200, 150]]}])
        self.assertTrue(result["admissible"])
        self.assertEqual(result["segments"][0]["status"], "admissible")

    def test_navigation_crossing_is_explained(self) -> None:
        result = self.evaluate([{"segment_id": "s1", "points": [[100, 500], [900, 500]]}])
        finding = result["segments"][0]["findings"][0]
        self.assertEqual(finding["rule"], "navigation_intersect")
        self.assertEqual(finding["version_id"], "nav-v1")
        self.assertIn("航道", finding["detail"])

    def test_cable_buffer_reports_required_and_actual_distance(self) -> None:
        result = self.evaluate([{"segment_id": "s1", "points": [[100, 285], [200, 285]]}])
        finding = result["segments"][0]["findings"][0]
        self.assertEqual(finding["rule"], "cable_buffer")
        self.assertEqual(finding["required_m"], "30")
        self.assertEqual(finding["actual_m"], "5.000")

    def test_restriction_window_and_commitment_expiry(self) -> None:
        window = {
            "window_id": "fw-1",
            "boundary_version_id": "sea-v1",
            "label": "渔业协商禁作期",
            "starts_at": "2026-10-01T00:00:00Z",
            "ends_at": "2026-11-30T23:59:59Z",
            "commitment_expires_at": "2027-12-31T00:00:00Z",
            "geometry": SEA,
        }
        overlapping = self.evaluate(
            [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            windows=[window],
            planned_starts_at="2026-10-15T00:00:00Z",
            planned_ends_at="2027-03-01T00:00:00Z",
        )
        self.assertEqual(overlapping["segments"][0]["findings"][0]["rule"], "restriction_window")
        expired = self.evaluate(
            [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            windows=[{**window, "commitment_expires_at": "2027-01-01T00:00:00Z"}],
            planned_starts_at="2026-10-15T00:00:00Z",
            planned_ends_at="2027-03-01T00:00:00Z",
        )
        self.assertEqual(expired["segments"][0]["findings"][0]["rule"], "commitment_expired")
        outside = self.evaluate(
            [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            windows=[window],
        )
        self.assertTrue(outside["admissible"])

    def test_missing_and_expired_authorization_are_plan_findings(self) -> None:
        missing = self.evaluate(
            [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            authorizations=[],
        )
        self.assertFalse(missing["admissible"])
        self.assertEqual(len(missing["plan_findings"]), 5)
        self.assertEqual(missing["plan_findings"][0]["rule"], "authorization_missing")
        expired = self.evaluate(
            [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            authorizations=authorizations_for("epc-north", "2027-01-01T00:00:00Z"),
        )
        self.assertEqual(expired["plan_findings"][0]["rule"], "authorization_expired")


class SeaPermitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = SeaPermitService(self.connection, self.clock)
        for user_id, role in (
            ("permit", "permit_admin"),
            ("builder", "construction"),
            ("keeper", "operations"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_boundary("permit", {"version_id": "sea-v1", "boundary_name": "北部海域", "kind": "sea_area", "geometry": SEA})
        self.service.register_boundary("permit", {"version_id": "nav-v1", "boundary_name": "中部航道", "kind": "navigation", "geometry": NAV})
        self.service.register_boundary("permit", {"version_id": "eco-v1", "boundary_name": "礁盘生态区", "kind": "ecology", "geometry": ECO})
        self.service.register_boundary("permit", {"version_id": "om-v1", "boundary_name": "运维可达区", "kind": "maintenance", "geometry": OM})
        self.service.register_boundary("permit", {"version_id": "cable-v1", "boundary_name": "既有送出电缆", "kind": "cable", "geometry": CABLE, "buffer_m": "30"})
        for kind, version_id in REFS.items():
            self.service.register_authorization("permit", {
                "authorization_id": f"auth-{kind}",
                "boundary_version_id": version_id,
                "grantor_party": "海域使用权人",
                "grantee_party": "epc-north",
                "commitment_expires_at": "2027-12-31T00:00:00Z",
            })

    def tearDown(self) -> None:
        self.connection.close()

    def compliant_plan(self, plan_id: str, key: str, party: str = "epc-north") -> dict[str, object]:
        return self.service.create_plan("builder", {
            "plan_id": plan_id,
            "kind": "construction",
            "party": party,
            "title": f"方案 {plan_id}",
            "segments": [{"segment_id": "seg-1", "points": [[100, 100], [200, 150]]}],
            "boundary_refs": dict(REFS),
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": key,
        })

    def progress_plan(self, plan_id: str, key: str, target: str) -> None:
        self.compliant_plan(plan_id, key)
        if target == "draft":
            return
        self.service.screen_plan("builder", plan_id)
        if target == "screened":
            return
        self.service.confirm_plan("builder", plan_id, 1)
        if target == "confirmed":
            return
        self.service.start_plan("builder", plan_id, 2)
        if target == "in_progress":
            return
        self.service.complete_plan("builder", plan_id, 3)

    def plan_state(self, plan_id: str) -> str:
        row = self.connection.execute("SELECT state FROM permit_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return row["state"]

    def test_plan_must_pin_every_boundary_kind(self) -> None:
        payload = {
            "plan_id": "p-x",
            "kind": "export",
            "party": "epc-north",
            "title": "缺类别",
            "segments": [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            "boundary_refs": {"sea_area": "sea-v1", "navigation": "nav-v1"},
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": "key-x",
        }
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("builder", payload)

    def test_plan_rejects_unknown_or_mismatched_versions(self) -> None:
        payload = {
            "plan_id": "p-y",
            "kind": "construction",
            "party": "epc-north",
            "title": "错版本",
            "segments": [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            "boundary_refs": {**REFS, "cable": "nav-v1"},
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": "key-y",
        }
        with self.assertRaises(ValidationFailed):
            self.service.create_plan("builder", payload)
        payload["boundary_refs"] = {**REFS, "cable": "no-such"}
        with self.assertRaises(NotFound):
            self.service.create_plan("builder", payload)

    def test_plan_creation_replay_and_payload_conflict(self) -> None:
        payload = {
            "plan_id": "p-idem",
            "kind": "construction",
            "party": "epc-north",
            "title": "幂等",
            "segments": [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
            "boundary_refs": dict(REFS),
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": "key-idem",
        }
        first = self.service.create_plan("builder", payload)
        self.assertEqual(first, self.service.create_plan("builder", payload))
        with self.assertRaises(Conflict):
            self.service.create_plan("builder", dict(payload, title="不同内容"))

    def test_screening_replay_is_stable(self) -> None:
        self.compliant_plan("p-screen", "key-screen")
        first = self.service.screen_plan("builder", "p-screen")
        second = self.service.screen_plan("builder", "p-screen")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["screening_id"], second["screening_id"])
        self.assertTrue(first["admissible"])

    def test_screening_explains_segment_exclusion(self) -> None:
        self.service.create_plan("builder", {
            "plan_id": "p-cross",
            "kind": "export",
            "party": "epc-north",
            "title": "穿越航道",
            "segments": [{"segment_id": "seg-cross", "points": [[100, 500], [900, 500]]}],
            "boundary_refs": dict(REFS),
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": "key-cross",
        })
        result = self.service.screen_plan("builder", "p-cross")
        self.assertFalse(result["admissible"])
        segment = result["segments"][0]
        self.assertEqual(segment["status"], "excluded")
        rules = {finding["rule"] for finding in segment["findings"]}
        self.assertIn("navigation_intersect", rules)
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("builder", "p-cross", 1)

    def test_full_lifecycle_to_completion(self) -> None:
        self.compliant_plan("p-life", "key-life")
        self.service.screen_plan("builder", "p-life")
        confirmed = self.service.confirm_plan("builder", "p-life", 1)
        self.assertEqual(confirmed["revision"], 2)
        started = self.service.start_plan("builder", "p-life", 2)
        self.assertEqual(started["state"], "in_progress")
        completed = self.service.complete_plan("builder", "p-life", 3)
        self.assertEqual(completed["state"], "completed")
        with self.assertRaises(InvalidState):
            self.service.screen_plan("builder", "p-life")

    def test_missing_authorization_blocks_confirmation(self) -> None:
        self.compliant_plan("p-other", "key-other", party="epc-other")
        result = self.service.screen_plan("builder", "p-other")
        self.assertFalse(result["admissible"])
        self.assertEqual(result["plan_findings"][0]["rule"], "authorization_missing")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("builder", "p-other", 1)

    def test_batch_import_is_all_or_nothing(self) -> None:
        payload = {
            "batch_id": "batch-atomic",
            "idempotency_key": "batch-key-atomic",
            "boundaries": [
                {"version_id": "sea-v2", "boundary_name": "北部海域", "kind": "sea_area", "geometry": SEA},
            ],
            "windows": [
                {
                    "window_id": "fw-bad",
                    "boundary_version_id": "no-such-version",
                    "label": "禁作期",
                    "starts_at": "2026-10-01T00:00:00Z",
                    "ends_at": "2026-11-30T00:00:00Z",
                    "commitment_expires_at": "2027-12-31T00:00:00Z",
                }
            ],
        }
        with self.assertRaises(ValidationFailed):
            self.service.import_batch("permit", payload)
        with self.assertRaises(NotFound):
            self.service.boundary_detail("permit", "sea-v2")

    def test_batch_import_replay_and_conflict(self) -> None:
        payload = {
            "batch_id": "batch-ok",
            "idempotency_key": "batch-key-ok",
            "boundaries": [
                {"version_id": "sea-v9", "boundary_name": "南部海域", "kind": "sea_area", "geometry": SEA},
            ],
            "authorizations": [
                {
                    "authorization_id": "auth-batch",
                    "boundary_version_id": "sea-v9",
                    "grantor_party": "海域使用权人",
                    "grantee_party": "epc-north",
                    "commitment_expires_at": "2027-12-31T00:00:00Z",
                }
            ],
        }
        first = self.service.import_batch("permit", payload)
        self.assertEqual(first, self.service.import_batch("permit", payload))
        changed = dict(payload, boundaries=[dict(payload["boundaries"][0], boundary_name="改名海域")])
        with self.assertRaises(Conflict):
            self.service.import_batch("permit", changed)
        detail = self.service.boundary_detail("permit", "sea-v9")
        self.assertEqual(detail["revision"], 1)
        self.assertEqual(detail["authorizations"][0]["authorization_id"], "auth-batch")

    def test_withdrawal_cascade_respects_construction_history(self) -> None:
        self.progress_plan("p-draft", "key-d", "draft")
        self.progress_plan("p-screened", "key-s", "screened")
        self.progress_plan("p-confirmed", "key-c", "confirmed")
        self.progress_plan("p-running", "key-r", "in_progress")
        self.progress_plan("p-done", "key-done", "completed")
        result = self.service.withdraw_boundary("permit", "nav-v1", "航道规划调整")
        self.assertEqual(result["affected"]["blocked"], ["p-confirmed", "p-draft", "p-screened"])
        self.assertEqual(result["affected"]["manual_review"], ["p-running"])
        self.assertEqual(self.plan_state("p-done"), "completed")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("builder", "p-confirmed", 2)
        with self.assertRaises(InvalidState):
            self.service.create_plan("builder", {
                "plan_id": "p-late",
                "kind": "construction",
                "party": "epc-north",
                "title": "引用已撤回版本",
                "segments": [{"segment_id": "s1", "points": [[100, 100], [200, 150]]}],
                "boundary_refs": dict(REFS),
                "planned_starts_at": "2027-01-15T00:00:00Z",
                "planned_ends_at": "2027-06-01T00:00:00Z",
                "idempotency_key": "key-late",
            })
        completed_view = self.service.plan_view("builder", "p-done")
        self.assertEqual(completed_view["state"], "completed")
        self.assertEqual(completed_view["boundary_refs"]["navigation"], "nav-v1")

    def test_revoke_authorization_cascade_only_hits_matching_party(self) -> None:
        self.progress_plan("p-north", "key-n", "in_progress")
        self.service.register_authorization("permit", {
            "authorization_id": "auth-other-sea",
            "boundary_version_id": "sea-v1",
            "grantor_party": "海域使用权人",
            "grantee_party": "epc-other",
            "commitment_expires_at": "2027-12-31T00:00:00Z",
        })
        self.compliant_plan("p-other", "key-o", party="epc-other")
        result = self.service.revoke_authorization("permit", "auth-sea_area", "主体违约")
        self.assertEqual(result["affected"]["manual_review"], ["p-north"])
        self.assertEqual(self.plan_state("p-other"), "draft")

    def test_manual_disposition_requires_explicit_repin(self) -> None:
        self.progress_plan("p-run", "key-run", "in_progress")
        self.service.withdraw_boundary("permit", "nav-v1", "航道规划调整")
        self.assertEqual(self.plan_state("p-run"), "manual_review")
        with self.assertRaises(Forbidden):
            self.service.resolve_plan("builder", "p-run", "terminate", "越权")
        self.service.register_boundary("permit", {"version_id": "nav-v2", "boundary_name": "中部航道", "kind": "navigation", "geometry": [[700, 0], [900, 0], [900, 1000], [700, 1000]]})
        self.service.register_authorization("permit", {
            "authorization_id": "auth-navigation-2",
            "boundary_version_id": "nav-v2",
            "grantor_party": "海事管理机构",
            "grantee_party": "epc-north",
            "commitment_expires_at": "2027-12-31T00:00:00Z",
        })
        resolved = self.service.resolve_plan(
            "keeper", "p-run", "repin", "人工处置:改用新航道版本",
            {**REFS, "navigation": "nav-v2"},
        )
        self.assertEqual(resolved["state"], "draft")
        self.assertEqual(resolved["boundary_refs"]["navigation"], "nav-v2")
        screening = self.service.screen_plan("builder", "p-run")
        self.assertTrue(screening["admissible"])

    def test_blocked_plan_resolution_and_terminal_state(self) -> None:
        self.progress_plan("p-block", "key-b", "screened")
        self.service.withdraw_boundary("permit", "nav-v1", "航道规划调整")
        self.assertEqual(self.plan_state("p-block"), "blocked")
        with self.assertRaises(Forbidden):
            self.service.resolve_plan("keeper", "p-block", "terminate", "越权")
        resolved = self.service.resolve_plan("builder", "p-block", "terminate", "放弃该方案")
        self.assertEqual(resolved["state"], "terminated")
        with self.assertRaises(InvalidState):
            self.service.resolve_plan("builder", "p-block", "terminate", "重复处置")

    def test_role_views_are_shaped_by_permission(self) -> None:
        self.service.create_plan("builder", {
            "plan_id": "p-view",
            "kind": "export",
            "party": "epc-north",
            "title": "视图方案",
            "segments": [{"segment_id": "seg-cross", "points": [[100, 500], [900, 500]]}],
            "boundary_refs": dict(REFS),
            "planned_starts_at": "2027-01-15T00:00:00Z",
            "planned_ends_at": "2027-06-01T00:00:00Z",
            "idempotency_key": "key-view",
        })
        self.service.screen_plan("builder", "p-view")
        construction_view = self.service.plan_view("builder", "p-view")
        self.assertIn("segments", construction_view)
        findings = construction_view["latest_screening"]["segments"][0]["findings"]
        self.assertTrue(any(item["rule"] == "navigation_intersect" for item in findings))
        operations_view = self.service.plan_view("keeper", "p-view")
        self.assertIn("restriction_windows", operations_view)
        auditor_view = self.service.plan_view("audit", "p-view")
        self.assertEqual(len(auditor_view["evidence"]["boundary_content"]), 5)
        self.assertEqual(len(auditor_view["evidence"]["screening_input_sha256"]), 64)
        permit_view = self.service.plan_view("permit", "p-view")
        self.assertNotIn("segments", permit_view)
        self.assertEqual(permit_view["state"], "screened")
        with self.assertRaises(Forbidden):
            self.service.screen_plan("audit", "p-view")
        with self.assertRaises(Forbidden):
            self.service.register_boundary("builder", {"version_id": "x-1", "boundary_name": "x", "kind": "ecology", "geometry": ECO})

    def test_audit_chain_detects_tampering(self) -> None:
        self.compliant_plan("p-audit", "key-audit")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE permit_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])
        with self.assertRaises(Forbidden):
            self.service.audit_chain("builder")

    def test_api_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        self.assertEqual(app.handle("POST", "/users", {}, b"{}").status, 422)
        created = app.handle("POST", "/users", {"X-Actor-Id": "any"}, b'{"user_id":"extra","display_name":"e","role":"auditor"}')
        self.assertEqual(created.status, 201)
        self.compliant_plan("p-api", "key-api")
        view = app.handle("GET", "/plans/p-api", {"X-Actor-Id": "builder"})
        self.assertEqual(view.status, 200)
        self.assertEqual(view.body["plan_id"], "p-api")
        denied = app.handle("GET", "/audit/chain", {"X-Actor-Id": "builder"})
        self.assertEqual(denied.status, 403)
        missing = app.handle("GET", "/plans/none", {"X-Actor-Id": "builder"})
        self.assertEqual(missing.status, 404)
        unknown = app.handle("GET", "/nope", {"X-Actor-Id": "builder"})
        self.assertEqual(unknown.status, 404)


class SeaPermitAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["batch_replay_stable"])
        self.assertFalse(result["screening_a_admissible"])
        self.assertTrue(result["screening_b_admissible"])
        self.assertEqual(result["withdrawal"]["affected"]["blocked"], ["plan-export-a"])
        self.assertEqual(result["withdrawal"]["affected"]["manual_review"], ["plan-build-b"])
        self.assertTrue(result["screening_b2_admissible"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
