"""001_initial_baseline: 建立基线表结构。"""
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL,
            ua VARCHAR(500) NOT NULL,
            viewport_w INTEGER NOT NULL,
            viewport_h INTEGER NOT NULL,
            proxy VARCHAR(300),
            daily_limit INTEGER NOT NULL,
            last_check_at DATETIME,
            last_publish_at DATETIME,
            created_at DATETIME NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            title VARCHAR(100) NOT NULL,
            content VARCHAR NOT NULL,
            note_type VARCHAR(10) NOT NULL,
            media_paths JSON NOT NULL,
            tags JSON NOT NULL,
            status VARCHAR(20) NOT NULL,
            schedule_mode VARCHAR(10) NOT NULL,
            scheduled_at DATETIME,
            published_at DATETIME,
            xhs_note_id VARCHAR(64),
            xhs_url VARCHAR(300),
            error VARCHAR,
            source VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts (id)
        );

        CREATE TABLE IF NOT EXISTS note_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            fetched_at DATETIME NOT NULL,
            impression INTEGER NOT NULL,
            read INTEGER NOT NULL,
            "like" INTEGER NOT NULL,
            collect INTEGER NOT NULL,
            comment INTEGER NOT NULL,
            share INTEGER NOT NULL,
            fans_gain INTEGER NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes (id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level VARCHAR(10) NOT NULL,
            source VARCHAR(30) NOT NULL,
            message VARCHAR NOT NULL,
            created_at DATETIME NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            "key" VARCHAR(50) PRIMARY KEY,
            value VARCHAR NOT NULL
        );
        """
    )
    conn.commit()

