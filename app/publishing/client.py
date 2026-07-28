from dataclasses import dataclass
from typing import Protocol

from app.publishing.guards import PublicationDraft


@dataclass(frozen=True)
class PublishedPost:
    url: str


class PublisherFailure(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class PublisherClient(Protocol):
    def publish(self, draft: PublicationDraft) -> PublishedPost:
        ...

    def verify_private(self, post: PublishedPost, draft: PublicationDraft) -> None:
        ...
