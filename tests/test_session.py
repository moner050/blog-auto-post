from pathlib import Path

from app.db.session import ensure_sqlite_parent


def test_sqlite_database_parent_is_created_before_migration(tmp_path: Path) -> None:
    """Removing directory preparation makes a configured SQLite development DB impossible to open."""
    database_file = tmp_path / "nested" / "automation.db"

    ensure_sqlite_parent(f"sqlite:///{database_file}")

    assert database_file.parent.is_dir()
