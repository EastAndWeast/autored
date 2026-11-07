"""003_p4_stats_tracking: 扩展账号采集追踪与笔记指标来源元数据。"""
import sqlite3
from ..runner import column_exists


def upgrade(conn: sqlite3.Connection) -> None:
    # 1. 扩展 accounts 采集时间与最近不可用原因
    account_cols = [
        ("last_stats_at", "DATETIME"),
        ("last_stats_error", "TEXT"),
    ]
    for col_name, col_type in account_cols:
        if not column_exists(conn, "accounts", col_name):
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type};")

    # 2. 扩展 note_stats 记录指标获取来源与匹配方式
    stat_cols = [
        ("source", "VARCHAR(20) DEFAULT 'dom'"),
        ("raw_matched_by", "VARCHAR(20)"),
    ]
    for col_name, col_type in stat_cols:
        if not column_exists(conn, "note_stats", col_name):
            conn.execute(f"ALTER TABLE note_stats ADD COLUMN {col_name} {col_type};")

    conn.commit()

