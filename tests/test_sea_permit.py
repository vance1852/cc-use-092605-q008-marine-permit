from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from sea_permit.api import JsonApplication
from sea_permit.clock import FrozenClock
from sea_permit.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from sea_permit.geometry import (
    path_distance_m,
    point_in_ring,
    segment_distance_m,
    segment_inside_ring,
    segment_touches_ring,
)
from sea_permit.service import PermitService


SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]

BOUNDARY = [[122.0, 30.0], [122.1, 30.0], [122.1, 30.1], [122.0, 30.1]]
OM_ZONE = [[121.99, 29.99], [122.11, 29.99], [122.11, 30.11], [121.99, 30.11]]
NAV_ZONE = [[122.05, 30.02], [122.06, 30.02], [122.06, 30.03], [122.05, 30.03]]
ECO_ZONE = [[122.08, 30.08], [122.09, 30.08], [122.09, 30.09], [122.08, 30.09]]
CABLE = [[122.0, 30.05], [122.1, 30.05]]


def polygon(coordinates: list[list[float]]) -> dict[str, object]:
    return {"type": "polygon", "coordinates": coordinates}


def line(coordinates: list[list[float]]) -> dict[str, object]:
    return {"type": "line", "coordinates": coordinates}


def segment(path: list[list[float]], starts: str = "2026-10-10T00:00:00Z", ends: str = "2026-10-12T00:00:00Z") -> dict[str, object]:
    return {"path": line(path), "starts_at": starts, "ends_at": ends}


class GeometryTests(unittest.TestCase):
    def test_point_in_ring_counts_boundary_as_inside(self) -> None:
        self.assertTrue(point_in_ring((0.5, 0.5), SQUARE))
        self.assertTrue(point_in_ring((0.0, 0.5), SQUARE))
        self.assertFalse(point_in_ring((1.5, 0.5), SQUARE))

    def test_segment_inside_ring_allows_boundary_touch(self) -> None:
        self.assertTrue(segment_inside_ring((0.2, 0.2), (0.8, 0.2), SQUARE))
        self.assertTrue(segment_inside_ring((0.0, 0.2), (0.0, 0.8), SQUARE))
        self.assertFalse(segment_inside_ring((0.2, 0.2), (1.2, 0.2), SQUARE))
        self.assertFalse(segment_inside_ring((0.2, 0.2), (0.8, 1.2), SQUARE))

    def test_segment_inside_concave_ring_detects_exit(self) -> None:
        concave = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (1.0, 0.5), (0.0, 2.0), (0.0, 0.0)]
        self.assertFalse(segment_inside_ring((0.2, 1.6), (1.8, 1.6), concave))
        self.assertTrue(segment_inside_ring((0.2, 0.2), (1.8, 0.2), concave))

    def test_segment_touches_ring_detects_boundary_contact(self) -> None:
        self.assertTrue(segment_touches_ring((0.5, 0.5), (0.9, 0.9), SQUARE))
        self.assertTrue(segment_touches_ring((1.0, 0.2), (1.0, 0.8), SQUARE))
        self.assertTrue(segment_touches_ring((0.5, 0.5), (1.5, 0.5), SQUARE))
        self.assertFalse(segment_touches_ring((1.2, 0.2), (1.4, 0.4), SQUARE))

    def test_segment_distance_m_uses_planar_projection(self) -> None:
        distance = segment_distance_m((122.0, 30.0), (122.1, 30.0), (122.05, 30.01), (122.06, 30.01))
        self.assertAlmostEqual(distance, 1113.2, places=1)
        self.assertEqual(segment_distance_m((0.0, 0.0), (1.0, 0.0), (0.5, -1.0), (0.5, 1.0)), 0.0)

    def test_path_distance_m_takes_minimum_over_subsegments(self) -> None:
        distance = path_distance_m(
            [(122.0, 30.0), (122.05, 30.0), (122.1, 30.0)],
            [(122.05, 30.02), (122.05, 30.03)],
        )
        self.assertAlmostEqual(distance, 2 * 1113.2, delta=1.0)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = PermitService(self.connection, self.clock)
        for user_id, role in (
            ("build", "constructor"),
            ("ops", "operator"),
            ("reg", "regulator"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def register_permit(self) -> None:
        self.service.create_permit("reg", {"permit_id": "hai-001", "title": "帆石海域使用许可", "responsible_party": "builder-one"})
        self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon(BOUNDARY)})
        self.service.register_restriction(
            "reg",
            "hai-001",
            {
                "window_id": "win-001",
                "version": 1,
                "kind": "fishery_closure",
                "starts_at": "2026-10-05T00:00:00Z",
                "ends_at": "2026-10-08T00:00:00Z",
                "note": "渔业协商禁作期",
            },
        )
        self.service.grant_authorization(
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
        self.service.register_commitment(
            "reg",
            "hai-001",
            {"commitment_id": "com-001", "version": 1, "summary": "数据有效承诺", "expires_at": "2027-01-01T00:00:00Z"},
        )

    def import_constraints(self) -> None:
        self.service.import_constraints(
            "reg",
            {
                "batch_id": "batch-001",
                "items": [
                    {"type": "zone", "zone_id": "zone-nav", "category": "navigation", "rule": "outside",
                     "name": "航道安全区", "geometry": polygon(NAV_ZONE)},
                    {"type": "zone", "zone_id": "zone-eco", "category": "ecology", "rule": "outside",
                     "name": "生态红线区", "geometry": polygon(ECO_ZONE)},
                    {"type": "zone", "zone_id": "zone-om", "category": "om", "rule": "inside",
                     "name": "运维覆盖区", "geometry": polygon(OM_ZONE)},
                    {"type": "cable", "cable_id": "cable-001", "name": "既有海缆",
                     "path": line(CABLE), "protection_distance_m": "500"},
                ],
            },
        )

    def create_plan(self, plan_id: str, segments: list[dict[str, object]], *, key: str | None = None) -> dict[str, object]:
        return self.service.create_plan(
            "build",
            {
                "plan_id": plan_id,
                "kind": "construction",
                "title": f"方案 {plan_id}",
                "permit_id": "hai-001",
                "permit_version": 1,
                "party_id": "builder-one",
                "segments": segments,
                "idempotency_key": key or f"key-{plan_id}",
            },
        )


class PermitRegistrationTests(ServiceTestBase):
    def test_boundary_versions_increment_and_reject_duplicate_content(self) -> None:
        self.service.create_permit("reg", {"permit_id": "hai-001", "title": "许可", "responsible_party": "builder-one"})
        first = self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon(BOUNDARY)})
        self.assertEqual(first["version"], 1)
        with self.assertRaises(Conflict):
            self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon(BOUNDARY)})
        shifted = [[lon + 0.01, lat] for lon, lat in BOUNDARY]
        second = self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon(shifted)})
        self.assertEqual(second["version"], 2)

    def test_registration_requires_version_and_active_permit(self) -> None:
        self.service.create_permit("reg", {"permit_id": "hai-001", "title": "许可", "responsible_party": "builder-one"})
        with self.assertRaises(NotFound):
            self.service.register_commitment(
                "reg",
                "hai-001",
                {"commitment_id": "com-1", "version": 9, "summary": "承诺", "expires_at": "2027-01-01T00:00:00Z"},
            )
        self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon(BOUNDARY)})
        self.service.revoke_permit("reg", "hai-001", "撤回")
        with self.assertRaises(InvalidState):
            self.service.register_boundary_version("reg", "hai-001", {"boundary": polygon([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])})
        with self.assertRaises(InvalidState):
            self.service.register_restriction(
                "reg",
                "hai-001",
                {"window_id": "w", "version": 1, "kind": "other", "starts_at": "2026-10-01T00:00:00Z",
                 "ends_at": "2026-10-02T00:00:00Z", "note": "n"},
            )

    def test_registrar_role_is_required(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_permit("build", {"permit_id": "hai-001", "title": "许可", "responsible_party": "p"})

    def test_permit_view_lists_versions_and_linkage(self) -> None:
        self.register_permit()
        view = self.service.permit_view("audit", "hai-001")
        self.assertEqual(view["state"], "active")
        self.assertEqual(len(view["versions"]), 1)
        self.assertEqual(view["versions"][0]["restriction_windows"][0]["kind"], "fishery_closure")
        self.assertEqual(view["versions"][0]["commitments"][0]["expires_at"], "2027-01-01T00:00:00Z")


class BatchImportTests(ServiceTestBase):
    def test_import_is_all_or_nothing(self) -> None:
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_constraints(
                "reg",
                {
                    "batch_id": "batch-bad",
                    "items": [
                        {"type": "zone", "zone_id": "zone-ok", "category": "navigation", "rule": "outside",
                         "name": "航道", "geometry": polygon(NAV_ZONE)},
                        {"type": "zone", "zone_id": "zone-bad", "category": "om", "rule": "outside",
                         "name": "规则错误", "geometry": polygon(OM_ZONE)},
                    ],
                },
            )
        self.assertIn("items[1]", str(caught.exception))
        count = self.connection.execute("SELECT COUNT(*) AS c FROM constraint_zones").fetchone()["c"]
        self.assertEqual(count, 0)
        batches = self.connection.execute("SELECT COUNT(*) AS c FROM import_batches").fetchone()["c"]
        self.assertEqual(batches, 0)

    def test_import_replay_is_stable_and_conflicts_on_different_content(self) -> None:
        self.register_permit()
        self.import_constraints()
        first = self.service.import_view("reg", "batch-001")
        payload = {
            "batch_id": "batch-001",
            "items": [
                {"type": "zone", "zone_id": "zone-nav", "category": "navigation", "rule": "outside",
                 "name": "航道安全区", "geometry": polygon(NAV_ZONE)},
                {"type": "zone", "zone_id": "zone-eco", "category": "ecology", "rule": "outside",
                 "name": "生态红线区", "geometry": polygon(ECO_ZONE)},
                {"type": "zone", "zone_id": "zone-om", "category": "om", "rule": "inside",
                 "name": "运维覆盖区", "geometry": polygon(OM_ZONE)},
                {"type": "cable", "cable_id": "cable-001", "name": "既有海缆",
                 "path": line(CABLE), "protection_distance_m": "500"},
            ],
        }
        replay = self.service.import_constraints("reg", payload)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["zones"], first["zones"])
        self.assertEqual(replay["cables"], first["cables"])
        changed = dict(payload, items=payload["items"][:1])
        with self.assertRaises(Conflict):
            self.service.import_constraints("reg", changed)

    def test_import_rejects_duplicate_zone_ids_atomically(self) -> None:
        self.register_permit()
        self.import_constraints()
        with self.assertRaises(Conflict):
            self.service.import_constraints(
                "reg",
                {
                    "batch_id": "batch-002",
                    "items": [
                        {"type": "zone", "zone_id": "zone-nav", "category": "navigation", "rule": "outside",
                         "name": "重复编号", "geometry": polygon(NAV_ZONE)},
                    ],
                },
            )
        batches = self.connection.execute("SELECT COUNT(*) AS c FROM import_batches WHERE batch_id='batch-002'").fetchone()["c"]
        self.assertEqual(batches, 0)


class PlanEvaluationTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.register_permit()
        self.import_constraints()

    def evaluate_codes(self, plan_id: str) -> dict[int, list[str]]:
        result = self.service.evaluate_plan("build", plan_id)
        return {item["segment_index"]: [reason["code"] for reason in item["reasons"]] for item in result["excluded"]}

    def test_clean_segment_becomes_candidate(self) -> None:
        self.create_plan("plan-ok", [segment([[122.02, 30.01], [122.03, 30.01]])])
        result = self.service.evaluate_plan("build", "plan-ok")
        self.assertEqual(result["candidates"], [0])
        self.assertEqual(result["excluded"], [])

    def test_segment_outside_sea_area_is_explained(self) -> None:
        self.create_plan("plan-out", [segment([[122.2, 30.01], [122.3, 30.01]])])
        codes = self.evaluate_codes("plan-out")
        self.assertIn("outside_boundary", codes[0])

    def test_navigation_and_ecology_zones_exclude_with_basis(self) -> None:
        self.create_plan(
            "plan-zones",
            [
                segment([[122.04, 30.025], [122.07, 30.025]]),
                segment([[122.085, 30.085], [122.085, 30.095]]),
            ],
        )
        codes = self.evaluate_codes("plan-zones")
        self.assertEqual(codes[0], ["inside_exclusion"])
        self.assertEqual(codes[1], ["inside_exclusion"])
        view = self.service.construction_view("build", "plan-zones")
        self.assertEqual(view["segments"][0]["exclusions"][0]["reference_id"], "zone-nav")
        self.assertEqual(view["segments"][1]["exclusions"][0]["reference_id"], "zone-eco")

    def test_cable_protection_distance_is_explained_with_measurement(self) -> None:
        self.create_plan("plan-cable", [segment([[122.05, 30.051], [122.06, 30.051]])])
        codes = self.evaluate_codes("plan-cable")
        self.assertEqual(codes[0], ["cable_protection"])
        view = self.service.construction_view("build", "plan-cable")
        message = view["segments"][0]["exclusions"][0]["message"]
        self.assertIn("111", message)
        self.assertIn("500", message)

    def test_fishery_closure_window_excludes_segment(self) -> None:
        self.create_plan(
            "plan-time",
            [segment([[122.02, 30.07], [122.03, 30.07]], "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z")],
        )
        codes = self.evaluate_codes("plan-time")
        self.assertEqual(codes[0], ["restriction_window"])

    def test_missing_om_boundary_fails_closed(self) -> None:
        self.connection.execute("UPDATE constraint_zones SET active=0 WHERE category='om'")
        self.create_plan("plan-no-om", [segment([[122.02, 30.01], [122.03, 30.01]])])
        codes = self.evaluate_codes("plan-no-om")
        self.assertIn("boundary_missing", codes[0])

    def test_authorization_and_commitment_are_checked_per_segment(self) -> None:
        self.create_plan(
            "plan-auth",
            [segment([[122.02, 30.01], [122.03, 30.01]], "2027-04-01T00:00:00Z", "2027-04-03T00:00:00Z")],
        )
        codes = self.evaluate_codes("plan-auth")
        self.assertIn("authorization_missing", codes[0])
        self.assertIn("commitment_expired", codes[0])

    def test_missing_commitment_is_reported(self) -> None:
        self.connection.execute("DELETE FROM permit_commitments")
        self.create_plan("plan-no-com", [segment([[122.02, 30.01], [122.03, 30.01]])])
        codes = self.evaluate_codes("plan-no-com")
        self.assertEqual(codes[0], ["commitment_missing"])

    def test_plan_must_pin_existing_version(self) -> None:
        with self.assertRaises(NotFound):
            self.service.create_plan(
                "build",
                {
                    "plan_id": "plan-v9",
                    "kind": "construction",
                    "title": "引用不存在版本",
                    "permit_id": "hai-001",
                    "permit_version": 9,
                    "party_id": "builder-one",
                    "segments": [segment([[122.02, 30.01], [122.03, 30.01]])],
                    "idempotency_key": "key-v9",
                },
            )

    def test_plan_creation_is_idempotent(self) -> None:
        payload = {
            "plan_id": "plan-idem",
            "kind": "export",
            "title": "幂等方案",
            "permit_id": "hai-001",
            "permit_version": 1,
            "party_id": "builder-one",
            "segments": [segment([[122.02, 30.01], [122.03, 30.01]])],
            "idempotency_key": "key-idem",
        }
        first = self.service.create_plan("build", payload)
        self.assertEqual(first, self.service.create_plan("build", payload))
        with self.assertRaises(Conflict):
            self.service.create_plan("build", dict(payload, title="不同内容"))


class PlanLifecycleTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.register_permit()
        self.import_constraints()

    def make_clean_plan(self, plan_id: str, segments: int = 1) -> None:
        paths = [
            [[122.02, 30.01], [122.03, 30.01]],
            [[122.03, 30.06], [122.04, 30.06]],
        ]
        self.create_plan(plan_id, [segment(paths[index]) for index in range(segments)])
        self.service.evaluate_plan("build", plan_id)

    def test_confirm_requires_all_segments_candidate(self) -> None:
        self.create_plan(
            "plan-mixed",
            [
                segment([[122.02, 30.01], [122.03, 30.01]]),
                segment([[122.04, 30.025], [122.07, 30.025]]),
            ],
        )
        self.service.evaluate_plan("build", "plan-mixed")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("build", "plan-mixed", 1)

    def test_confirm_requires_evaluation(self) -> None:
        self.create_plan("plan-unevaluated", [segment([[122.02, 30.01], [122.03, 30.01]])])
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("build", "plan-unevaluated", 1)

    def test_full_lifecycle_to_completed(self) -> None:
        self.make_clean_plan("plan-life", segments=2)
        confirmed = self.service.confirm_plan("build", "plan-life", 1)
        self.assertEqual(confirmed["revision"], 2)
        started = self.service.start_plan("ops", "plan-life", 2)
        self.assertEqual(started["state"], "in_progress")
        self.service.complete_segment("ops", "plan-life", 0)
        with self.assertRaises(InvalidState):
            self.service.complete_plan("ops", "plan-life", 3)
        self.service.complete_segment("ops", "plan-life", 1)
        done = self.service.complete_plan("ops", "plan-life", 3)
        self.assertEqual(done["state"], "completed")

    def test_revocation_blocks_drafts_and_sends_active_plans_to_manual_review(self) -> None:
        self.make_clean_plan("plan-draft")
        self.make_clean_plan("plan-confirmed")
        self.service.confirm_plan("build", "plan-confirmed", 1)
        self.make_clean_plan("plan-running", segments=2)
        self.service.confirm_plan("build", "plan-running", 1)
        self.service.start_plan("ops", "plan-running", 2)
        self.service.complete_segment("ops", "plan-running", 0)
        self.make_clean_plan("plan-done")
        self.service.confirm_plan("build", "plan-done", 1)
        self.service.start_plan("ops", "plan-done", 2)
        self.service.complete_segment("ops", "plan-done", 0)
        self.service.complete_plan("ops", "plan-done", 3)

        result = self.service.revoke_permit("reg", "hai-001", "许可撤回")
        self.assertEqual(result["blocked_plan_ids"], ["plan-draft"])
        self.assertEqual(len(result["manual_case_ids"]), 2)

        draft = self.service.construction_view("build", "plan-draft")
        self.assertEqual(draft["state"], "blocked")
        running = self.service.construction_view("build", "plan-running")
        self.assertEqual(running["state"], "manual_review")
        self.assertEqual(running["segments"][0]["state"], "completed")
        self.assertIsNotNone(running["segments"][0]["completed_at"])
        done = self.service.construction_view("build", "plan-done")
        self.assertEqual(done["state"], "completed")

        with self.assertRaises(InvalidState):
            self.service.start_plan("ops", "plan-confirmed", 2)
        with self.assertRaises(InvalidState):
            self.service.revoke_permit("reg", "hai-001", "重复撤回")

    def test_revoked_permit_blocks_new_plans_and_confirms(self) -> None:
        self.make_clean_plan("plan-pending")
        self.service.revoke_permit("reg", "hai-001", "许可撤回")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("build", "plan-pending", 1)
        with self.assertRaises(InvalidState):
            self.create_plan("plan-late", [segment([[122.02, 30.01], [122.03, 30.01]])])

    def test_manual_halt_keeps_completed_segments(self) -> None:
        self.make_clean_plan("plan-run", segments=2)
        self.service.confirm_plan("build", "plan-run", 1)
        self.service.start_plan("ops", "plan-run", 2)
        self.service.complete_segment("ops", "plan-run", 0)
        revocation = self.service.revoke_permit("reg", "hai-001", "许可撤回")
        case_id = revocation["manual_case_ids"][0]
        resolved = self.service.resolve_manual_case("reg", case_id, {"action": "halt", "note": "停止施工"})
        self.assertEqual(resolved["plan_state"], "blocked")
        view = self.service.construction_view("build", "plan-run")
        self.assertEqual(view["segments"][0]["state"], "completed")
        with self.assertRaises(InvalidState):
            self.service.resolve_manual_case("reg", case_id, {"action": "halt", "note": "重复处置"})

    def test_manual_repin_is_explicit_and_requires_reevaluation(self) -> None:
        self.make_clean_plan("plan-migrate")
        self.service.confirm_plan("build", "plan-migrate", 1)
        self.service.create_permit("reg", {"permit_id": "hai-002", "title": "替代许可", "responsible_party": "builder-one"})
        self.service.register_boundary_version("reg", "hai-002", {"boundary": polygon(BOUNDARY)})
        self.service.grant_authorization(
            "reg",
            "hai-002",
            {"authorization_id": "auth-002", "version": 1, "party_id": "builder-one", "scope": "both",
             "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2027-03-01T00:00:00Z"},
        )
        self.service.register_commitment(
            "reg",
            "hai-002",
            {"commitment_id": "com-002", "version": 1, "summary": "承诺", "expires_at": "2027-01-01T00:00:00Z"},
        )
        revocation = self.service.revoke_permit("reg", "hai-001", "许可撤回")
        case_id = revocation["manual_case_ids"][0]
        resolved = self.service.resolve_manual_case(
            "reg",
            case_id,
            {"action": "repin", "note": "迁移到替代许可", "permit_id": "hai-002", "permit_version": 1},
        )
        self.assertEqual(resolved["plan_state"], "draft")
        view = self.service.construction_view("build", "plan-migrate")
        self.assertEqual(view["permit"]["permit_id"], "hai-002")
        self.assertEqual(view["segments"][0]["state"], "pending")
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("build", "plan-migrate", view["revision"])
        result = self.service.evaluate_plan("build", "plan-migrate")
        self.assertEqual(result["candidates"], [0])
        confirmed = self.service.confirm_plan("build", "plan-migrate", view["revision"])
        self.assertEqual(confirmed["state"], "confirmed")

    def test_repin_rejects_revoked_target(self) -> None:
        self.make_clean_plan("plan-x")
        self.service.confirm_plan("build", "plan-x", 1)
        self.service.create_permit("reg", {"permit_id": "hai-003", "title": "另一个许可", "responsible_party": "builder-one"})
        self.service.register_boundary_version("reg", "hai-003", {"boundary": polygon(BOUNDARY)})
        self.service.revoke_permit("reg", "hai-003", "预先撤回")
        revocation = self.service.revoke_permit("reg", "hai-001", "许可撤回")
        with self.assertRaises(InvalidState):
            self.service.resolve_manual_case(
                "reg",
                revocation["manual_case_ids"][0],
                {"action": "repin", "note": "目标不可用", "permit_id": "hai-003", "permit_version": 1},
            )


class ViewAndAuditTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.register_permit()
        self.import_constraints()
        self.create_plan(
            "plan-view",
            [
                segment([[122.02, 30.01], [122.03, 30.01]]),
                segment([[122.04, 30.025], [122.07, 30.025]]),
            ],
        )
        self.service.evaluate_plan("build", "plan-view")

    def test_views_are_scoped_by_role(self) -> None:
        construction = self.service.construction_view("build", "plan-view")
        self.assertIn("party_id", construction)
        self.assertEqual(construction["segments"][1]["exclusions"][0]["code"], "inside_exclusion")
        om = self.service.om_view("ops", "plan-view")
        self.assertNotIn("party_id", om)
        self.assertEqual(om["om_boundaries"][0]["zone_id"], "zone-om")
        self.assertEqual(om["segments"][1]["exclusions"][0]["reference_id"], "zone-nav")
        audit = self.service.audit_view("audit", "plan-view")
        self.assertIn("input_sha256", audit["segments"][0])
        self.assertTrue(any(event["event_type"] == "plan.evaluated" for event in audit["events"]))

    def test_cross_role_views_are_forbidden(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.construction_view("ops", "plan-view")
        with self.assertRaises(Forbidden):
            self.service.om_view("build", "plan-view")
        with self.assertRaises(Forbidden):
            self.service.audit_view("build", "plan-view")
        with self.assertRaises(Forbidden):
            self.service.plan_view("reg", "plan-view")

    def test_plan_view_dispatches_by_role(self) -> None:
        self.assertEqual(self.service.plan_view("build", "plan-view")["view"], "construction")
        self.assertEqual(self.service.plan_view("ops", "plan-view")["view"], "om")
        self.assertEqual(self.service.plan_view("audit", "plan-view")["view"], "audit")

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE sea_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_audit_chain_requires_auditor(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.audit_chain("build")

    def test_api_smoke(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/plans/plan-view", {"X-Actor-Id": "ops"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["view"], "om")
        forbidden = app.handle("GET", "/audit/chain", {"X-Actor-Id": "ops"})
        self.assertEqual(forbidden.status, 403)
        missing = app.handle("GET", "/plans/none", {"X-Actor-Id": "ops"})
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
