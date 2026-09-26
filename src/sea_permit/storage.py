"""海域许可联动服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sea_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('constructor','operator','regulator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sea_permits (
    permit_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    responsible_party TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
    revoke_reason TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permit_versions (
    permit_id TEXT NOT NULL REFERENCES sea_permits(permit_id),
    version INTEGER NOT NULL,
    boundary_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES sea_users(user_id),
    registered_at TEXT NOT NULL,
    PRIMARY KEY(permit_id, version),
    UNIQUE(permit_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS restriction_windows (
    window_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(permit_id, version) REFERENCES permit_versions(permit_id, version),
    CHECK(ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_restriction_version
ON restriction_windows(permit_id, version, starts_at);

CREATE TABLE IF NOT EXISTS party_authorizations (
    authorization_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    party_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('construction','export','both')),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(permit_id, version) REFERENCES permit_versions(permit_id, version),
    CHECK(valid_until > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_authorization_party
ON party_authorizations(permit_id, version, party_id);

CREATE TABLE IF NOT EXISTS permit_commitments (
    commitment_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    summary TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(permit_id, version) REFERENCES permit_versions(permit_id, version)
);

CREATE INDEX IF NOT EXISTS idx_commitment_version
ON permit_commitments(permit_id, version, expires_at);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS constraint_zones (
    zone_id TEXT PRIMARY KEY,
    category TEXT NOT NULL CHECK(category IN ('sea_area','navigation','ecology','om')),
    rule TEXT NOT NULL CHECK(rule IN ('inside','outside')),
    name TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    source_batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zones_category
ON constraint_zones(category, active, zone_id);

CREATE TABLE IF NOT EXISTS cable_corridors (
    cable_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path_json TEXT NOT NULL,
    protection_distance_m TEXT NOT NULL,
    source_batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sea_plans (
    plan_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('construction','export')),
    title TEXT NOT NULL,
    permit_id TEXT NOT NULL REFERENCES sea_permits(permit_id),
    permit_version INTEGER NOT NULL,
    party_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','in_progress','completed','blocked','manual_review')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES sea_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(permit_id, permit_version) REFERENCES permit_versions(permit_id, version)
);

CREATE INDEX IF NOT EXISTS idx_plans_permit_state
ON sea_plans(permit_id, state);

CREATE TABLE IF NOT EXISTS plan_segments (
    plan_id TEXT NOT NULL REFERENCES sea_plans(plan_id),
    segment_index INTEGER NOT NULL,
    path_json TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','candidate','excluded','completed')),
    completed_at TEXT,
    PRIMARY KEY(plan_id, segment_index),
    CHECK(ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS segment_evaluations (
    plan_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    permit_id TEXT NOT NULL,
    permit_version INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('candidate','excluded')),
    reasons_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    evaluated_by TEXT NOT NULL REFERENCES sea_users(user_id),
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, segment_index, permit_id, permit_version),
    FOREIGN KEY(plan_id, segment_index) REFERENCES plan_segments(plan_id, segment_index)
);

CREATE TABLE IF NOT EXISTS manual_cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES sea_plans(plan_id),
    trigger TEXT NOT NULL,
    prior_state TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_cases_plan
ON manual_cases(plan_id, state);

CREATE TABLE IF NOT EXISTS sea_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS sea_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sea_audit_entity
ON sea_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
