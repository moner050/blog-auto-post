from pathlib import Path

import pytest

from app.core.settings import Settings
from app.publishing.guards import PublicationDraft, PublicationPreflightError, validate_preflight


def configured_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "auto_publish_enabled": True,
        "tistory_production_enabled": True,
        "tistory_expected_blog_name": "생활꿀팁",
        "tistory_allowed_category": "테스트",
        "tistory_profile_path": tmp_path / "profile",
    }
    values.update(overrides)
    return Settings(**values)


def valid_draft(tmp_path: Path, **overrides: object) -> PublicationDraft:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"fake image")
    values: dict[str, object] = {
        "title": "정부24 등본 저장 방법",
        "body_html": "<h1>등본 저장</h1><p>확인할 내용입니다.</p>",
        "tags": ["정부24", "등본"],
        "category": "테스트",
        "target_blog_name": "생활꿀팁",
        "visibility": "PRIVATE",
        "thumbnail_path": thumbnail,
    }
    values.update(overrides)
    return PublicationDraft(**values)


def test_preflight_rejects_public_visibility_before_browser_use(tmp_path: Path) -> None:
    """Changing the private-only rule must stop an accidental public post."""
    settings = configured_settings(tmp_path)
    draft = valid_draft(tmp_path, visibility="PUBLIC")

    with pytest.raises(PublicationPreflightError, match="PRIVATE"):
        validate_preflight(settings, draft)


def test_preflight_accepts_any_category_without_filtering(tmp_path: Path) -> None:
    """Any category (e.g. 홈텍스, 정부24, 법원) should pass preflight without restriction."""
    settings = configured_settings(tmp_path, tistory_allowed_category="테스트")
    draft = valid_draft(tmp_path, category="임의카테고리")

    # Should not raise any error
    validate_preflight(settings, draft)


def test_preflight_rejects_wrong_blog(tmp_path: Path) -> None:
    """Removing target blog checks must not permit a post to another blog."""
    settings = configured_settings(tmp_path)
    draft = valid_draft(tmp_path, target_blog_name="다른 블로그")

    with pytest.raises(PublicationPreflightError, match="target blog"):
        validate_preflight(settings, draft)


def test_preflight_rejects_disabled_publish_switch(tmp_path: Path) -> None:
    """Ignoring the kill switch would allow an operator-disabled worker to publish."""
    settings = configured_settings(tmp_path, auto_publish_enabled=False)

    with pytest.raises(PublicationPreflightError, match="disabled"):
        validate_preflight(settings, valid_draft(tmp_path))


def test_preflight_rejects_missing_blog_configuration(tmp_path: Path) -> None:
    """Allowing blank target blog name should fail preflight."""
    settings = configured_settings(tmp_path, tistory_expected_blog_name="")
    draft = valid_draft(tmp_path, target_blog_name="")

    with pytest.raises(PublicationPreflightError, match="must be configured"):
        validate_preflight(settings, draft)
