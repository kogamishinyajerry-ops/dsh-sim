"""引擎与会话工厂（CONVENTIONS §1：URL 从 DSH_SIM_DATABASE_URL 读；默认 sqlite 文件 var/dsh_sim.db）。

SQLite 下通过 begin 事件把事务提升为 BEGIN IMMEDIATE，使 claim 的
"锁行-校验配额-分配租约-提交"短事务在单文件库上可串行化。
PostgreSQL 部署时该事件自动不生效，claim 应改用 SELECT ... FOR UPDATE SKIP LOCKED
（queue/service.py 中有对应注释）。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dsh_sim.db.models import Base
from dsh_sim.resources import state_dir

ENV_DATABASE_URL = "DSH_SIM_DATABASE_URL"


def default_db_path() -> Path:
    """默认开发库路径：仓库 `var/`（源码运行）或用户状态目录（安装后）。

    报告 §五：旧实现用 `Path(__file__).parents[3]` 推算仓库根，wheel 安装后
    会落到 site-packages 上层（既非预期位置，也不该往那里写）。
    """
    return state_dir() / "dsh_sim.db"


def database_url() -> str:
    url = os.environ.get(ENV_DATABASE_URL)
    if url:
        return url
    return f"sqlite:///{default_db_path().as_posix()}"


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # noqa: ANN001, ANN202
            # SQLite：写事务立即取保留锁，避免 claim 并发下先读后写丢失更新。
            # PG 下不使用本机制（见 queue/service.claim 注释）。
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """显式会话边界：成功提交，异常回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "ENV_DATABASE_URL",
    "database_url",
    "init_db",
    "make_engine",
    "make_session_factory",
    "session_scope",
]
