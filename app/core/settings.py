from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
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
    # Sonar Chat Completions는 2026-09-27에 종료되어 Agent API(/v1/agent)가 기본이다. 'sonar'는 임시 우회용.
    perplexity_api_mode: Literal["agent", "sonar"] = "agent"
    perplexity_country: str = "KR"  # 웹 검색 지역(ISO 3166-1 alpha-2). 비우면 지정하지 않는다.

    # 관리자 대시보드 보호: 허용 Host(쉼표 구분, 포트 제외) / HTTP Basic 인증(비밀번호를 설정하면 모든 요청에 요구)
    dashboard_allowed_hosts: str = "127.0.0.1,localhost,::1"
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    # 발행 잡이 RUNNING 상태로 이 시간(초)보다 오래 멈춰 있으면 중복 발행을 피하려고 수동 확인 상태로 돌린다.
    # 락을 갱신하는 하트비트가 없으므로, 정상적인 발행(브라우저 단계 1~몇 분)이 끝나기 전에 만료되지 않게 최소 600초를 요구한다.
    publish_lock_ttl_seconds: int = Field(default=1800, ge=600)

    # 글 생성 파이프라인 설정 (비워 두면 기본값 사용)
    article_model: str = ""  # 비어 있으면 perplexity_model 사용
    article_temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    article_max_tokens: int = Field(default=8000, ge=1000, le=32000)
    article_max_revisions: int = Field(default=1, ge=0, le=3)
    article_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)  # 호출 1회당 총 대기 상한(재시도 포함)
    article_style_rules_path: Path = Path("configs/tistory_blog_style_rules.yaml")
    # clickbait: 주제 탐색의 어그로·고CTR 지향을 글 제목에도 적용 / persona: 스타일 규칙 YAML의 차분한 제목 규칙
    article_title_style: Literal["clickbait", "persona"] = "clickbait"

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
