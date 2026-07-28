from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Publishing stays disabled unless explicitly enabled."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # MySQL 개별 접속 설정
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""

    # 전체 데이터베이스 URL (직접 지정 시 우선 적용)
    database_url: str = ""

    auto_publish_enabled: bool = False
    tistory_production_enabled: bool = False
    tistory_expected_blog_name: str = ""
    tistory_allowed_category: str = ""
    tistory_profile_path: Path = Path("storage/browser_profiles/production")
    tistory_write_url: str = "https://www.tistory.com"
    tistory_selectors_path: Path = Path("configs/tistory_selectors.yaml")
    screenshots_path: Path = Path("storage/screenshots")
    traces_path: Path = Path("storage/traces")

    # Perplexity LLM Settings
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    perplexity_base_url: str = "https://api.perplexity.ai"

    @model_validator(mode="after")
    def assemble_database_url(self) -> Settings:
        """database_url이 비어 있고 MySQL 정보가 주어지면 database_url을 자동 생성."""
        if not self.database_url and self.mysql_user and self.mysql_database:
            password_part = f":{self.mysql_password}" if self.mysql_password else ""
            self.database_url = (
                f"mysql+pymysql://{self.mysql_user}{password_part}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
            )
        return self
