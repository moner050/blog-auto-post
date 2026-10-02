from dataclasses import dataclass
from typing import Protocol

from app.publishing.guards import PublicationDraft


@dataclass(frozen=True)
class PublishedPost:
    url: str


class PublisherFailure(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, post_url: str | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        # 글이 이미 만들어졌을 수 있는 실패(검증 실패 등)에서 알아낸 글 주소. 워커가 result_url로 보존한다.
        self.post_url = post_url


class PublisherClient(Protocol):
    def publish(self, draft: PublicationDraft) -> PublishedPost:
        ...

    def verify_private(self, post: PublishedPost, draft: PublicationDraft) -> str | None:
        """비공개가 아니면 PublisherFailure를 던진다. 비공개로 판단한 근거(예: 'HTTP 404')를 돌려줄 수 있다(선택)."""
        ...
