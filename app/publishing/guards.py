from dataclasses import dataclass
from pathlib import Path

from app.core.settings import Settings


class PublicationPreflightError(ValueError):
    pass


@dataclass(frozen=True)
class PublicationDraft:
    title: str
    body_html: str
    tags: list[str]
    category: str
    target_blog_name: str
    visibility: str
    thumbnail_path: Path


def validate_preflight(settings: Settings, draft: PublicationDraft) -> None:
    """프리플라이트 검증: 카테고리 필터링 제약 없이 모든 카테고리 포스팅 즉시 등록 허용."""
    if not settings.auto_publish_enabled or not settings.tistory_production_enabled:
        raise PublicationPreflightError("publishing is disabled")
    if not settings.tistory_expected_blog_name:
        raise PublicationPreflightError("target blog must be configured")
    if draft.visibility != "PRIVATE":
        raise PublicationPreflightError("only PRIVATE visibility is allowed")
    if draft.target_blog_name != settings.tistory_expected_blog_name:
        raise PublicationPreflightError("target blog does not match configured target blog")
    if not draft.title.strip() or not draft.body_html.strip() or not draft.tags:
        raise PublicationPreflightError("draft is missing required content")
    if not draft.thumbnail_path.is_file():
        raise PublicationPreflightError("thumbnail file does not exist")
