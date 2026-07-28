from __future__ import annotations

from app.core.settings import Settings


def test_settings_database_url_from_mysql_vars():
    settings = Settings(
        _env_file=None,
        database_url="",
        mysql_host="localhost",
        mysql_port=3306,
        mysql_user="myuser",
        mysql_password="mypassword",
        mysql_database="mydb",
    )
    assert settings.database_url == "mysql+pymysql://myuser:mypassword@localhost:3306/mydb?charset=utf8mb4"


def test_settings_direct_database_url_takes_precedence():
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        mysql_host="localhost",
        mysql_user="myuser",
        mysql_database="mydb",
    )
    assert settings.database_url == "sqlite:///:memory:"
