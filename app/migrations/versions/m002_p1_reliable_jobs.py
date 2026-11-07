"""002_p1_reliable_jobs: 扩展账号/笔记元数据并新增持久任务与发布尝试表。"""
import sqlite3
from ..runner import column_exists


def upgrade(conn: sqlite3.Connection) -> None:
    # 1. 扩展 accounts
    account_columns = [
        ("platform_region", "VARCHAR(20) DEFAULT 'unknown'"),
        ("creator_base", "VARCHAR(100)"),
        ("platform_user_id", "VARCHAR(64)"),
        ("platform_nickname", "VARCHAR(100)"),
        ("timezone", "VARCHAR(50) DEFAULT 'Asia/Shanghai'"),
    ]
    for col_name, col_type in account_columns:
        if not column_exists(conn, "accounts", col_name):
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type};")

    # 2. 扩展 notes
    note_columns = [
        ("revision", "INTEGER DEFAULT 1"),
        ("submitted_at", "DATETIME"),
        ("confirmed_at", "DATETIME"),
        ("platform_status", "VARCHAR(30)"),
        ("verification_status", "VARCHAR(30)"),
        ("xhs_publish_url", "VARCHAR(500)"),
    ]
    for col_name, col_type in note_columns:
        if not column_exists(conn, "notes", col_name):
            conn.execute(f"ALTER TABLE notes ADD COLUMN {col_name} {col_type};")

    # 3. 创建 publish_jobs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            note_revision INTEGER NOT NULL DEFAULT 1,
            content_snapshot JSON NOT NULL,
            mode VARCHAR(20) NOT NULL DEFAULT 'immediate',
            scheduled_at DATETIME,
            effective_at DATETIME,
            timezone VARCHAR(50) NOT NULL DEFAULT 'Asia/Shanghai',
            status VARCHAR(30) NOT NULL DEFAULT 'draft',
            idempotency_key VARCHAR(128),
            payload_hash VARCHAR(64),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner VARCHAR(64),
            lease_expires_at DATETIME,
            cancel_requested BOOLEAN NOT NULL DEFAULT 0,
            last_error_code VARCHAR(50),
            last_error_message TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE CASCADE,
            FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publish_jobs_note_id ON publish_jobs(note_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publish_jobs_account_id ON publish_jobs(account_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publish_jobs_status ON publish_jobs(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publish_jobs_idempotency_key ON publish_jobs(idempotency_key);")

    # 4. 创建 publish_attempts
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            note_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            domain VARCHAR(100),
            stage VARCHAR(50) NOT NULL DEFAULT 'init',
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            platform_note_id VARCHAR(64),
            success_url VARCHAR(500),
            detail_url VARCHAR(500),
            error_code VARCHAR(50),
            error_message TEXT,
            verification_result VARCHAR(100),
            created_at DATETIME NOT NULL,
            FOREIGN KEY (job_id) REFERENCES publish_jobs (id) ON DELETE CASCADE,
            FOREIGN KEY (note_id) REFERENCES notes (id) ON DELETE CASCADE,
            FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publish_attempts_job_id ON publish_attempts(job_id);")
    conn.commit()

