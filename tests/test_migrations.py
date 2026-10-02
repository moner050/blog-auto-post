from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


def test_initial_alembic_migration_creates_private_publishing_schema(tmp_path) -> None:
    """Removing a migration table would make a fresh deployment fail after initialization."""
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")

    tables = set(inspect(create_engine(database_url)).get_table_names())
    assert {
        "articles",
        "article_versions",
        "media_assets",
        "publish_jobs",
        "jobs",
        "job_runs",
        "system_settings",
        "topic_candidates",
    }.issubset(tables)


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _create_topic_candidates_by_hand(database_url: str, *, with_reason: bool = True) -> None:
    """MySQL에서 이전 실행이 테이블만 만들고 버전 기록 전에 실패한 상태(또는 손으로 만든 테이블)를 흉내 낸다."""
    reason = "reason TEXT NOT NULL, " if with_reason else ""
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE topic_candidates (id INTEGER PRIMARY KEY, batch_id VARCHAR(36) NOT NULL, "
                "topic VARCHAR(255) NOT NULL, topic_hash VARCHAR(64) NOT NULL, category VARCHAR(100) NOT NULL, "
                f"{reason}sources_json JSON NOT NULL, status VARCHAR(20) NOT NULL, article_id INTEGER UNIQUE, "
                "error_message TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        if with_reason:
            connection.execute(
                text(
                    "INSERT INTO topic_candidates (id, batch_id, topic, topic_hash, category, reason, sources_json, status, "
                    "created_at, updated_at) VALUES (1, 'b', '기존 주제', 'h', '생활꿀팁', '이유', '[]', 'NEW', "
                    "'2026-10-01 00:00:00', '2026-10-01 00:00:00')"
                )
            )
    engine.dispose()


def test_upgrade_adopts_a_topic_candidates_table_that_already_exists(tmp_path) -> None:
    """init-db가 'Table topic_candidates already exists'로 실패하던 DB: 테이블은 있는데 버전 기록은 이전 단계."""
    database_url = f"sqlite:///{tmp_path / 'partial.db'}"
    config = _config(database_url)
    command.upgrade(config, "20260727_01")
    _create_topic_candidates_by_hand(database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # 다시 실행해도 그대로

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260730_02"
        assert connection.execute(text("SELECT topic FROM topic_candidates")).scalar_one() == "기존 주제"  # 기존 데이터 유지
    assert "ix_topic_candidates_batch_id" in {index["name"] for index in inspect(engine).get_indexes("topic_candidates")}
    engine.dispose()


def test_upgrade_refuses_an_existing_topic_candidates_table_with_missing_columns(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'broken.db'}"
    config = _config(database_url)
    command.upgrade(config, "20260727_01")
    _create_topic_candidates_by_hand(database_url, with_reason=False)

    with pytest.raises(RuntimeError, match="missing columns: reason"):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260727_01"
    engine.dispose()
