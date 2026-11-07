"""轻量数据库迁移引擎:保证多环境增量升级、版本追踪与回退安全。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            applied_at DATETIME NOT NULL
        );
        """
    )
    conn.commit()


def get_applied_versions(conn_or_path: sqlite3.Connection | Path | str) -> set[int]:
    if isinstance(conn_or_path, (str, Path)):
        conn = sqlite3.connect(str(conn_or_path))
        try:
            return get_applied_versions(conn)
        finally:
            conn.close()
    conn = conn_or_path
    ensure_migration_table(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in cursor.fetchall()}


def record_migration(conn: sqlite3.Connection, version: int, name: str) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, datetime.now().isoformat(sep=" ")),
    )
    conn.commit()


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def run_migrations(db_path: Path | str) -> int:
    """按顺序应用未执行的迁移脚本,返回应用的迁移数量。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    from .versions import get_all_migrations

    migrations = get_all_migrations()
    applied_count = 0

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_migration_table(conn)
        applied = get_applied_versions(conn)

        for version, name, upgrade_fn in migrations:
            if version not in applied:
                # 事务内执行
                upgrade_fn(conn)
                record_migration(conn, version, name)
                applied_count += 1
    finally:
        conn.close()

    return applied_count

