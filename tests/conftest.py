from __future__ import annotations

import os
from pathlib import Path
import socket

import pytest

from app.core.settings import Settings

# 테스트는 개발자의 실제 설정(.env, 셸에 내보낸 환경 변수)을 쓰지 않는다. 운영 DB나 API 키가 섞여 들어오면 테스트가
# 실제 데이터를 건드리거나 유료 호출을 할 수 있다. 앱 모듈은 import 시점에 Settings()를 읽으므로, 어떤 테스트 모듈이
# import되기 전에(conftest는 가장 먼저 로드된다) 설정에 쓰이는 환경 변수를 모두 지우고 .env 읽기를 끈 뒤 필요한 값만 넣는다.
for _field in Settings.model_fields:
    os.environ.pop(_field.upper(), None)
Settings.model_config["env_file"] = None
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERPLEXITY_API_KEY"] = ""
# 운영 기본값은 엄격하게 두고, 테스트 클라이언트의 기본 Host("testserver")만 허용한다.
os.environ["DASHBOARD_ALLOWED_HOSTS"] = "127.0.0.1,localhost,::1,testserver"

from app.llm.style import StyleProfile, load_style_profile  # noqa: E402
from tests.article_helpers import MINI_RULES  # noqa: E402

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard(real):  # noqa: ANN001, ANN202
    def guarded(self, address, *args, **kwargs):  # noqa: ANN001, ANN202
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise RuntimeError(f"테스트가 외부 네트워크({host})로 연결을 시도했습니다. 요청은 모킹해야 합니다.")
        return real(self, address, *args, **kwargs)

    return guarded


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """모킹을 빠뜨린 테스트가 실제 API로 나가는 사고를 막는다(루프백은 이벤트 루프·TestClient용으로 허용)."""
    monkeypatch.setattr(socket.socket, "connect", _guard(_real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(_real_connect_ex))


@pytest.fixture(scope="session")
def mini_rules_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("style") / "mini_rules.yaml"
    path.write_text(MINI_RULES, encoding="utf-8")
    return path


@pytest.fixture
def mini_profile(mini_rules_path: Path) -> StyleProfile:
    return load_style_profile(mini_rules_path, "주민등록등본 발급 방법", "정부지원·민원")
