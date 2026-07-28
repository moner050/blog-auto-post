from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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
    }.issubset(tables)
