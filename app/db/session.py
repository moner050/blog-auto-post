from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    ensure_sqlite_parent(database_url)
    # 끊어진 커넥션 자동 감지(pool_pre_ping) 및 1시간 주기 커넥션 재연결(pool_recycle) 옵션 적용
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    return sessionmaker(engine, expire_on_commit=False)


def ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)
