from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from app.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_external_connections_are_blocked_during_tests() -> None:
    """모킹을 빠뜨린 테스트가 실제 API로 나가지 못하게 conftest가 막는다(문서용 TEST-NET 주소, DNS 조회 없음)."""
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match="외부 네트워크"):
            sock.connect(("203.0.113.5", 443))
        with pytest.raises(RuntimeError, match="외부 네트워크"):
            sock.connect_ex(("203.0.113.5", 443))


def test_tests_never_inherit_the_developers_settings() -> None:
    settings = Settings()

    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:" and settings.database_url == "sqlite:///:memory:"
    assert os.environ["PERPLEXITY_API_KEY"] == "" and settings.perplexity_api_key == ""
    assert Settings.model_config["env_file"] is None


def test_conftest_discards_a_hostile_shell_environment() -> None:
    """셸에 운영 DB·API 키·타임아웃이 내보내져 있어도 conftest를 import하는 것만으로 모두 지워진다(새 프로세스로 확인)."""
    env = {
        **os.environ,
        "DATABASE_URL": "mysql+pymysql://root:pw@db.invalid/prod",
        "PERPLEXITY_API_KEY": "pplx-real-looking-key",
        "ARTICLE_TIMEOUT_SECONDS": "300",
        "DASHBOARD_PASSWORD": "from-the-shell",
    }
    code = "; ".join(
        [
            "import tests.conftest",
            "from app.core.settings import Settings",
            "s = Settings()",
            "print(s.database_url, repr(s.perplexity_api_key), s.article_timeout_seconds, repr(s.dashboard_password))",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite:///:memory: '' 120.0 ''"
