"""数据库模型:账号、笔记、数据快照、日志、设置。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pathlib import Path

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import config

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def rebind_db(db_path: Path | str) -> None:
    """动态重绑数据库连接与会话工厂(供沙箱隔离与测试套件使用)。"""
    global engine, SessionLocal
    target_path = Path(db_path).resolve()
    config.configure_data_dir(target_path.parent)
    engine = create_engine(
        f"sqlite:///{target_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SessionLocal.configure(bind=engine)


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.now()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="未命名账号")
    # pending=待扫码 logging_in=登录中 online=在线 expired=登录失效
    status: Mapped[str] = mapped_column(String(20), default="pending")
    platform_region: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown | cn | global
    creator_base: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    platform_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    platform_nickname: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    ua: Mapped[str] = mapped_column(String(500), default="")
    viewport_w: Mapped[int] = mapped_column(default=1440)
    viewport_h: Mapped[int] = mapped_column(default=900)
    proxy: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    daily_limit: Mapped[int] = mapped_column(default=config.DEFAULT_DAILY_LIMIT)
    last_check_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_publish_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_stats_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_stats_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)

    notes: Mapped[list["Note"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def profile_dir(self) -> str:
        return str(config.PROFILES_DIR / f"account_{self.id}")


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    title: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(default="")
    note_type: Mapped[str] = mapped_column(String(10), default="image")  # image|video
    media_paths: Mapped[list] = mapped_column(JSON, default=list)       # data/uploads 相对路径
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # draft|pending|queued|publishing|published|failed|platform_scheduled
    status: Mapped[str] = mapped_column(String(20), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # immediate=立即 local=本地定时(到点立即发) platform=平台原生定时
    schedule_mode: Mapped[str] = mapped_column(String(10), default="immediate")
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    platform_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # 审核中 | 已发布 | 违规等
    verification_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # verified | unverified | uncertain
    xhs_note_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    xhs_url: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    xhs_publish_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(default=None)
    source: Mapped[str] = mapped_column(String(20), default="web")  # web|obsidian|ai
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    account: Mapped[Account] = relationship(back_populates="notes")
    stats: Mapped[list["NoteStat"]] = relationship(
        back_populates="note",
        order_by="NoteStat.fetched_at",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["PublishJob"]] = relationship(
        back_populates="note",
        cascade="all, delete-orphan",
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    note_revision: Mapped[int] = mapped_column(Integer, default=1)
    content_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # immediate | local | platform
    mode: Mapped[str] = mapped_column(String(20), default="immediate")
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    effective_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    # draft | queued | publishing | platform_scheduled | submitted | verifying | published | failed | cancelled | overdue | needs_login
    status: Mapped[str] = mapped_column(String(30), default="draft")
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    payload_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    note: Mapped["Note"] = relationship(back_populates="jobs")
    attempts: Mapped[list["PublishAttempt"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id", ondelete="CASCADE"))
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(default=now)
    ended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    domain: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # init | media_uploaded | draft_saved | submitting | redirecting | completed
    stage: Mapped[str] = mapped_column(String(50), default="init")
    # running | success | failed | uncertain
    status: Mapped[str] = mapped_column(String(20), default="running")
    platform_note_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    success_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    detail_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_result: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)

    job: Mapped["PublishJob"] = relationship(back_populates="attempts")


class NoteStat(Base):
    __tablename__ = "note_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"))
    fetched_at: Mapped[datetime] = mapped_column(default=now)
    impression: Mapped[int] = mapped_column(default=0)  # 曝光
    read: Mapped[int] = mapped_column(default=0)        # 阅读/观看
    like: Mapped[int] = mapped_column(default=0)
    collect: Mapped[int] = mapped_column(default=0)
    comment: Mapped[int] = mapped_column(default=0)
    share: Mapped[int] = mapped_column(default=0)
    fans_gain: Mapped[int] = mapped_column(default=0)   # 涨粉
    source: Mapped[str] = mapped_column(String(20), default="dom")  # api | dom
    raw_matched_by: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # platform_id | title

    note: Mapped[Note] = relationship(back_populates="stats")


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info|warn|error
    source: Mapped[str] = mapped_column(String(30), default="app")
    message: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=now)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(default="")


def init_db(db_path: Path | str | None = None) -> None:
    target_db = Path(db_path) if db_path else config.DB_PATH
    from .migrations import run_migrations
    run_migrations(target_db)
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA busy_timeout=5000;")
    except Exception:
        pass


def log(level: str, source: str, message: str, session: Session | None = None) -> None:
    """写一条运行日志(支持复用已有会话或独立短会话)。"""
    try:
        entry = LogEntry(level=level, source=source, message=message[:2000])
        if session is not None:
            session.add(entry)
        else:
            with SessionLocal() as s:
                s.add(entry)
                s.commit()
    except Exception:
        pass
