"""海域许可联动服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS permit_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('permit_admin','construction','operations','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boundary_versions (
    version_id TEXT PRIMARY KEY,
    boundary_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sea_area','navigation','ecology','maintenance','cable')),
    geometry_json TEXT NOT NULL,
    buffer_m TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL CHECK(revision > 0),
    supersedes_version_id TEXT REFERENCES boundary_versions(version_id),
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','withdrawn')),
    withdrawn_at TEXT,
    withdrawn_by TEXT REFERENCES permit_users(user_id),
    withdraw_reason TEXT,
    registered_by TEXT NOT NULL REFERENCES permit_users(user_id),
    registered_at TEXT NOT NULL,
    UNIQUE(boundary_name, kind, revision)
);

CREATE INDEX IF NOT EXISTS idx_boundary_name
ON boundary_versions(boundary_name, kind, revision);

CREATE TABLE IF NOT EXISTS restriction_windows (
    window_id TEXT PRIMARY KEY,
    boundary_version_id TEXT NOT NULL REFERENCES boundary_versions(version_id),
    label TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    commitment_expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','cancelled')),
    registered_by TEXT NOT NULL REFERENCES permit_users(user_id),
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_windows_version
ON restriction_windows(boundary_version_id, state);

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    boundary_version_id TEXT NOT NULL REFERENCES boundary_versions(version_id),
    grantor_party TEXT NOT NULL,
    grantee_party TEXT NOT NULL,
    commitment_expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
    revoked_at TEXT,
    revoked_by TEXT REFERENCES permit_users(user_id),
    registered_by TEXT NOT NULL REFERENCES permit_users(user_id),
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_authorizations_version
ON authorizations(boundary_version_id, grantee_party, state);

CREATE TABLE IF NOT EXISTS permit_plans (
    plan_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('construction','export')),
    party TEXT NOT NULL,
    title TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    boundary_refs_json TEXT NOT NULL,
    planned_starts_at TEXT NOT NULL,
    planned_ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','screened','confirmed','in_progress','completed','blocked','manual_review','terminated')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES permit_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_state
ON permit_plans(state, kind);

CREATE TABLE IF NOT EXISTS plan_screenings (
    screening_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES permit_plans(plan_id),
    plan_revision INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    admissible INTEGER NOT NULL CHECK(admissible IN (0,1)),
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES permit_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, plan_revision, input_sha256)
);

CREATE TABLE IF NOT EXISTS plan_dispositions (
    disposition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES permit_plans(plan_id),
    action TEXT NOT NULL CHECK(action IN ('repin','terminate')),
    note TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES permit_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permit_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS permit_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_permit_audit_entity
ON permit_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
