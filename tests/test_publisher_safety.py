"""발행 어댑터(TistoryPublisher)의 안전 장치 테스트.

실제 브라우저·네트워크 없이 순수 파이썬 가짜 Page/Locator로 검증한다. 가짜는 어댑터가 실제로 부르는 Playwright 메서드만 구현하고,
요소는 코드가 쓰는 선택자 문자열과 '정확히 같은' 키로 등록한다(선택자를 바꾸면 이 테스트가 깨져 다시 확인하게 된다).

주의: 가짜는 실제 Tistory 에디터가 아니라 '이런 UI라면 이렇게 동작해야 한다'는 가정을 담은 모델이다.
실제 UI와의 일치는 비공개 글 1건으로 하는 수동 드라이런으로 확인해야 한다(어댑터 상단의 A1~A12 가정 참고).
"""

from __future__ import annotations

import html
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.publishing.tistory as tistory
from app.content.static import DraftArticleInput, StaticArticleInput, register_draft_article, register_private_article
from app.core.settings import Settings
from app.db.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Base,
    Job,
    JobStatus,
    MediaAsset,
    PublishJob,
    PublishStatus,
)
from app.jobs.worker import PublisherWorker
from app.publishing.client import PublishedPost, PublisherFailure
from app.publishing.guards import PublicationDraft
from app.publishing.tistory import TistoryPublisher

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- 어댑터가 쓰는 선택자 (코드와 정확히 같은 문자열) ----------------------------------------------------------
TITLE_SELECTOR = "#post-title-inp, textarea[placeholder*='제목'], input[placeholder*='제목']"
TAG_SELECTOR = "#tag-inp, input[placeholder*='태그'], input[placeholder*='#태그']"
COMPLETE_ID = "#publish-layer-btn"
COMPLETE_SELECTOR = "button:has-text('완료')"
OLD_COMPLETE_SELECTOR = "#publish-btn, button:has-text('완료'), button:has-text('발행')"  # 제거된 8단계 후보(회귀 방지용 상수)
FILE_SELECTOR = "input[type='file']"
PRIVATE_RADIO = "input[type='radio'][value='0']"
PRIVATE_ID = "#visibility-private"
PRIVATE_LABEL = "label:has-text('비공개')"
PRIVATE_LOOSE = "input[value='0']"
PROTECTED_RADIO = "input[type='radio'][value='15']"
PUBLIC_RADIO = "input[type='radio'][value='20']"
FINAL_PRIVATE_TEXT = "button:has-text('비공개')"
FINAL_PUBLISH_TEXT = "button:has-text('발행하기')"
FINAL_SAVE_TEXT = "button:has-text('저장')"
FINAL_LAYER_ID = "#layer-publish-btn"
FINAL_CONFIRM_CLASS = ".btn_confirm"
FINAL_CONFIRM_BUTTON = "button.btn-confirm"
FINAL_PRIMARY = "button.btn-primary"
LEGACY_PUBLIC_BUTTON = "button:has-text('공개 발행')"  # 제거된 후보. 회귀를 잡으려고 가짜 UI에는 계속 둔다.
FINAL_CANDIDATES = (
    FINAL_PRIVATE_TEXT,
    FINAL_PUBLISH_TEXT,
    FINAL_SAVE_TEXT,
    FINAL_LAYER_ID,
    FINAL_CONFIRM_CLASS,
    FINAL_CONFIRM_BUTTON,
    FINAL_PRIMARY,
)

TITLE = "정부24 등본 저장 방법"
BODY_HTML = "<h1>등본 저장</h1><p>확인할 내용입니다. 본문은 여러 단어로 구성됩니다.</p>"
POST_URL = "https://blog.tistory.com/123"


# =================================================================================================================
# 가짜 Page / Locator
# =================================================================================================================
def _resolve(value: Any) -> Any:
    return value() if callable(value) else value


class FakeElement:
    """Locator와 요소를 겸하는 가짜. 어댑터가 부르는 메서드만 구현한다."""

    def __init__(
        self,
        name: str,
        *,
        text: Any = "",
        visible: Any = True,
        attrs: dict[str, Any] | None = None,
        checked: Any = None,
        value: str = "",
        on_click: Callable[[], None] | None = None,
        click_error: Exception | None = None,
        fill_effective: bool = True,
    ) -> None:
        self.name = name
        self.text = text
        self.visible = visible
        self.attrs = dict(attrs or {})
        self.checked = checked  # None이면 체크박스/라디오가 아니다 → is_checked()가 Playwright처럼 오류를 낸다
        self.value = value
        self.on_click = on_click
        self.click_error = click_error
        self.fill_effective = fill_effective
        self.clicks: list[dict[str, Any]] = []
        self.fills: list[str] = []
        self.uploads: list[str] = []
        self.pressed: list[str] = []
        self.evaluations: list[tuple[str, Any]] = []
        self.evaluate_result: Any = True  # 클릭 가드 설치 결과(설치 직후 비공개 상태인가)
        self.evaluate_error: Exception | None = None

    @property
    def first(self) -> FakeElement:
        return self

    def element_handle(self, **kwargs: Any) -> FakeElement:
        self.handle_kwargs = kwargs
        return self

    def evaluate(self, script: str, arg: Any = None, **kwargs: Any) -> Any:
        self.evaluations.append((script, arg))
        self.evaluate_kwargs = kwargs
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return self.evaluate_result

    def count(self) -> int:
        return 1

    def all(self) -> list[FakeElement]:
        return [self]

    def is_visible(self) -> bool:
        return bool(_resolve(self.visible))

    def inner_text(self) -> str:
        return str(_resolve(self.text))

    def get_attribute(self, name: str) -> Any:
        return _resolve(self.attrs.get(name))

    def is_checked(self) -> bool:
        state = _resolve(self.checked)
        if state is None:
            raise PlaywrightError("Not a checkbox or radio button")
        return bool(state)

    def click(self, **kwargs: Any) -> None:
        self.clicks.append(kwargs)
        if self.click_error is not None:
            raise self.click_error
        if not self.is_visible():
            raise PlaywrightError(f"{self.name} is not visible")
        if self.on_click is not None:
            self.on_click()

    def fill(self, value: str) -> None:
        self.fills.append(value)
        if self.fill_effective:
            self.value = value

    def input_value(self) -> str:
        return self.value

    def set_input_files(self, path: str) -> None:
        self.uploads.append(path)

    def press(self, key: str) -> None:
        self.pressed.append(key)


class MissingElement:
    """일치하는 요소가 없는 Locator.first: is_visible()은 False, 그 밖의 동작은 Playwright처럼 실패한다."""

    def __init__(self, selector: str) -> None:
        self.selector = selector

    def is_visible(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise PlaywrightError(f"Timeout: no element for {self.selector!r} ({name})")

        return fail


class FakeLocator:
    def __init__(self, selector: str, elements: list[FakeElement]) -> None:
        self.selector = selector
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    @property
    def first(self) -> FakeElement | MissingElement:
        return self._elements[0] if self._elements else MissingElement(self.selector)

    def all(self) -> list[FakeElement]:
        return list(self._elements)


class FakePage:
    """선택자 문자열을 키로 요소를 등록해 두는 가짜 Page. 등록하지 않은 선택자는 빈 Locator(count()==0)다."""

    def __init__(self, url: str = "https://blog.tistory.com/manage/newpost/?type=post") -> None:
        self.url = url
        self.registry: dict[str, list[tuple[FakeElement, Callable[[], bool] | None]]] = {}
        self.texts: dict[str, list[FakeElement]] = {}
        self.content_by_url: dict[str, str] = {}
        self.default_content = "<html><body></body></html>"
        self.status_by_url: dict[str, int] = {}
        self.status_sequences: dict[str, list[int]] = {}  # 방문할 때마다 앞에서부터 하나씩 쓴다(없으면 status_by_url)
        self.content_sequences: dict[str, list[str]] = {}  # 마지막 값은 계속 쓴다
        self.redirects: dict[str, str] = {}
        self.goto_errors: dict[str, Exception] = {}
        self.visited: list[str] = []
        self.keys: list[str] = []
        self.waits: list[int] = []
        self.url_waits: list[int | None] = []
        self.load_state_waits: list[str] = []
        self.load_state_error: Exception | None = None
        self.handlers: dict[str, Any] = {}
        self.init_scripts: list[str] = []
        self.on_wait: Callable[[], None] | None = None
        self.keyboard = SimpleNamespace(press=self.keys.append)
        # 에디터 모델(page.evaluate): 주입 스크립트는 인자를 받고, 되읽기 스크립트는 인자가 없다.
        self.editor: dict[str, Any] = {"codemirror": None, "textareas": [""], "tinymce": None}
        self.guard_blocked: Any = 0  # 가드가 취소한 이벤트 수(window.__tistoryGuardBlocked)
        self.guard_blocked_error: Exception | None = None
        self.inject_effective = True
        self.inject_error: Exception | None = None
        self.readback_error: Exception | None = None
        self.title_element: FakeElement | None = None
        self.clobber_title = False

    # 등록 도우미 -----------------------------------------------------------------------------------------------
    def add(self, selector: str, element: FakeElement, when: Callable[[], bool] | None = None) -> FakeElement:
        self.registry.setdefault(selector, []).append((element, when))
        return element

    def add_text(self, text: str, element: FakeElement) -> FakeElement:
        self.texts.setdefault(text, []).append(element)
        return element

    # Playwright Page API ---------------------------------------------------------------------------------------
    def locator(self, selector: str) -> FakeLocator:
        matching = [element for element, when in self.registry.get(selector, []) if when is None or when()]
        return FakeLocator(selector, matching)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(f"text={text}", list(self.texts.get(text, [])))

    def get_by_placeholder(self, text: str) -> FakeLocator:
        return FakeLocator(f"placeholder={text}", [])

    def get_by_role(self, role: str, name: str = "") -> FakeLocator:
        return FakeLocator(f"role={role}:{name}", [])

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait()

    def wait_for_url(self, predicate: Callable[[str], bool], timeout: int | None = None) -> None:
        """Playwright처럼: 주소가 조건을 만족하면 바로 돌아오고, 아니면 시간 초과 오류를 낸다."""
        self.url_waits.append(timeout)
        if not predicate(self.url):
            raise PlaywrightError(f"Timeout {timeout}ms exceeded waiting for URL (now {self.url})")

    def wait_for_load_state(self, state: str = "load", timeout: int | None = None) -> None:
        self.load_state_waits.append(state)
        if self.load_state_error is not None:
            raise self.load_state_error

    def goto(self, url: str, wait_until: str | None = None) -> Any:
        self.visited.append(url)
        if url in self.goto_errors:
            raise self.goto_errors[url]
        self.url = self.redirects.get(url, url)
        contents = self.content_sequences.get(url)
        if contents:
            self.content_by_url[self.url] = contents.pop(0) if len(contents) > 1 else contents[0]
        statuses = self.status_sequences.get(url)
        status = statuses.pop(0) if statuses else self.status_by_url.get(url, 200)
        return SimpleNamespace(status=status)

    def content(self) -> str:
        return self.content_by_url.get(self.url, self.default_content)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script == tistory._GUARD_BLOCKED_SCRIPT:
            if self.guard_blocked_error is not None:
                raise self.guard_blocked_error
            return self.guard_blocked
        if arg is not None:  # 본문 주입
            if self.inject_error is not None:
                raise self.inject_error
            if self.inject_effective:
                self.editor["codemirror"] = arg
                self.editor["textareas"] = [arg]
            if self.clobber_title and self.title_element is not None:
                self.title_element.value = arg  # 제목 textarea까지 덮어쓰는 주입 사고
            return None
        if self.readback_error is not None:
            raise self.readback_error
        return json.loads(json.dumps(self.editor))

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class FakeLayer:
    """발행 레이어와 서버 쪽 결과를 흉내 낸다.

    - 공개 범위 라디오: 비공개(0) / 보호(15) / 공개(20). 기본 선택은 default(공개).
    - 최종 버튼 문구는 선택에 따라 '비공개 저장' / '공개 발행'으로 바뀐다(final_label을 주면 고정 문구).
    - 최종 버튼을 누르는 순간의 선택값이 서버에 저장된다 → published. 비공개가 아니면 '공개 글이 올라간' 것이다.
    markup: radio(보이는 라디오) | hidden_input_label(숨은 input + 보이는 label) | custom(#visibility-private 커스텀 컨트롤)
            | missing(비공개 옵션 없음: UI가 '나만 보기'로 바뀐 경우)
    """

    def __init__(
        self,
        page: FakePage,
        *,
        markup: str = "radio",
        default: str = "20",
        click_effective: bool = True,
        shared_group: bool = True,
        final_label: str | None = None,
        after_publish_url: str = POST_URL,
        opened: bool = True,
    ) -> None:
        self.page = page
        self.markup = markup
        self.selected = default
        self.click_effective = click_effective
        self.shared_group = shared_group
        self.fixed_label = final_label
        self.after_publish_url = after_publish_url
        self.is_open = opened
        self.decoy_private_checked = False
        self.published: list[tuple[str, str]] = []  # (서버에 저장된 공개 범위 값, 눌린 버튼 문구)
        visible = lambda: self.is_open  # noqa: E731

        self.protected = FakeElement(
            "protected-radio", visible=visible, attrs={"type": "radio", "value": "15"},
            checked=lambda: self.selected == "15", on_click=self._chooser("15"),
        )
        self.public = FakeElement(
            "public-radio", visible=visible, attrs={"type": "radio", "value": "20"},
            checked=lambda: self.selected == "20", on_click=self._chooser("20"),
        )
        page.add(PROTECTED_RADIO, self.protected)
        page.add(PUBLIC_RADIO, self.public)

        self.private: FakeElement | None = None
        self.label: FakeElement | None = None
        private_checked = (lambda: self.selected == "0") if shared_group else (lambda: self.decoy_private_checked)
        if markup in ("radio", "hidden_input_label"):
            self.private = FakeElement(
                "private-radio",
                visible=(visible if markup == "radio" else False),
                attrs={"type": "radio", "value": "0"},
                checked=private_checked,
                on_click=self._chooser("0"),
            )
            page.add(PRIVATE_RADIO, self.private)
            page.add(PRIVATE_LOOSE, self.private)
        if markup == "hidden_input_label":
            # label.is_checked()는 Playwright처럼 label이 가리키는 컨트롤의 상태를 따라간다.
            self.label = FakeElement(
                "private-label", text="비공개", visible=visible, attrs={"for": "open0"},
                checked=private_checked, on_click=self._chooser("0"),
            )
            page.add(PRIVATE_LABEL, self.label)
        if markup == "custom":
            self.private = FakeElement(
                "custom-private", visible=visible, checked=None,
                attrs={"role": "radio", "aria-checked": "false", "class": "opt"},
                on_click=self._chooser("0"),
            )
            page.add(PRIVATE_ID, self.private)
        if markup == "missing":
            page.add("label:has-text('나만 보기')", FakeElement("only-me-label", text="나만 보기", visible=visible))

        self.final = FakeElement("final-button", text=self._final_label, visible=visible, on_click=self._publish)
        page.add(FINAL_PRIVATE_TEXT, self.final, when=lambda: "비공개" in self.final.inner_text())
        page.add(FINAL_PUBLISH_TEXT, self.final, when=lambda: "발행하기" in self.final.inner_text())
        page.add(FINAL_SAVE_TEXT, self.final, when=lambda: "저장" in self.final.inner_text())
        page.add(LEGACY_PUBLIC_BUTTON, self.final, when=lambda: "공개 발행" in self.final.inner_text())
        page.add(FINAL_PRIMARY, self.final)

    # 동작 모델 -------------------------------------------------------------------------------------------------
    def _chooser(self, value: str) -> Callable[[], None]:
        def choose() -> None:
            if not self.click_effective:  # 예: 라벨 클릭의 기본 동작을 막는 UI
                return
            if value == "0" and not self.shared_group:
                self.decoy_private_checked = True
            else:
                self.selected = value
            if self.markup == "custom" and value == "0" and self.private is not None:
                self.private.attrs["aria-checked"] = "true"

        return choose

    def _final_label(self) -> str:
        if self.fixed_label is not None:
            return self.fixed_label
        return {"0": "비공개 저장", "15": "보호 발행"}.get(self.selected, "공개 발행")

    def _publish(self) -> None:
        self.published.append((self.selected, self._final_label()))
        self.page.url = self.after_publish_url

    def open(self) -> None:
        self.is_open = True

    def add_decoy_button(self, selector: str, text: str, *, first: bool = False) -> FakeElement:
        """같은 선택자에 걸리는 다른 버튼(예: has-text('저장')에 걸리는 '임시저장'). first=True면 DOM 순서상 앞에 둔다."""
        button = FakeElement(f"decoy:{text}", text=text, visible=lambda: self.is_open, on_click=self._publish)
        if first:
            self.page.registry.setdefault(selector, []).insert(0, (button, None))
        else:
            self.page.add(selector, button)
        return button

    @property
    def final_clicks(self) -> int:
        """최종 발행 후보로 쓰일 수 있는 모든 요소에 대한 클릭 수."""
        elements = [self.final]
        for selector in (*FINAL_CANDIDATES, LEGACY_PUBLIC_BUTTON):
            elements.extend(element for element, _ in self.page.registry.get(selector, []))
        return sum(len(element.clicks) for element in {id(e): e for e in elements}.values())


# =================================================================================================================
# 공통 준비물
# =================================================================================================================
def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,  # 실제 .env를 읽지 않는다
        "database_url": "sqlite:///:memory:",
        "auto_publish_enabled": True,
        "tistory_production_enabled": True,
        "tistory_expected_blog_name": "lmh 의 일상",
        "tistory_allowed_category": "테스트",
        "tistory_profile_path": tmp_path / "profile",
        "tistory_write_url": "https://blog.tistory.com/manage/newpost/?type=post",
        "tistory_selectors_path": REPO_ROOT / "configs" / "tistory_selectors.yaml",
    }
    values.update(overrides)
    return Settings(**values)


def make_draft(tmp_path: Path, **overrides: Any) -> PublicationDraft:
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"image")
    values: dict[str, Any] = {
        "title": TITLE,
        "body_html": BODY_HTML,
        "tags": ["정부24", "등본"],
        "category": "테스트",
        "target_blog_name": "lmh 의 일상",
        "visibility": "PRIVATE",
        "thumbnail_path": thumbnail,
    }
    values.update(overrides)
    return PublicationDraft(**values)


def make_publisher(tmp_path: Path) -> TistoryPublisher:
    return TistoryPublisher(make_settings(tmp_path))


def post_html(title: str = TITLE, body: str = BODY_HTML, extra: str = "") -> str:
    escaped = html.escape(title)
    return (
        f"<!doctype html><html><head><title>{escaped}</title></head>"
        f"<body><h1>{escaped}</h1><div class='article'>{body}</div>{extra}</body></html>"
    )


def layer_page(**options: Any) -> tuple[FakePage, FakeLayer]:
    page = FakePage()
    return page, FakeLayer(page, **options)


class FakeContext:
    def __init__(self, page: FakePage, close_error: Exception | None = None) -> None:
        self.page = page
        self.close_error = close_error
        self.closed = False
        self.handlers: dict[str, Any] = {}

    def new_page(self) -> FakePage:
        return self.page

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(self, page: FakePage | None, version: str | None = "151.0.7922.34") -> None:
        self.page = page
        self.closed = False
        self.new_page_calls = 0
        self.page_options: list[dict[str, Any]] = []
        self.version = version

    def new_page(self, **options: Any) -> FakePage:
        self.new_page_calls += 1
        self.page_options.append(options)
        assert self.page is not None, "익명 페이지를 준비하지 않았다"
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeRuntime:
    """sync_playwright()가 돌려주는 객체 흉내: chromium.launch_persistent_context / chromium.launch."""

    def __init__(
        self,
        page: FakePage | None = None,
        anonymous_page: FakePage | None = None,
        close_error: Exception | None = None,
        *,
        persistent_error: Exception | None = None,
        launch_error: Exception | None = None,
    ):
        self.context = FakeContext(page, close_error) if page is not None else None
        self.browser = FakeBrowser(anonymous_page)
        self.persistent_error = persistent_error
        self.launch_error = launch_error
        self.launches: list[str] = []
        self.chromium = SimpleNamespace(launch_persistent_context=self._persistent, launch=self._launch)

    def _persistent(self, *args: Any, **kwargs: Any) -> FakeContext:
        self.launches.append("persistent")
        if self.persistent_error is not None:
            raise self.persistent_error
        assert self.context is not None
        return self.context

    def _launch(self, *args: Any, **kwargs: Any) -> FakeBrowser:
        self.launches.append("anonymous")
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser


def install_runtime(monkeypatch: pytest.MonkeyPatch, runtime: FakeRuntime) -> None:
    class Manager:
        def __enter__(self) -> FakeRuntime:
            return runtime

        def __exit__(self, *exc_info: Any) -> bool:
            return False

    monkeypatch.setattr(tistory, "sync_playwright", lambda: Manager())


class Scenario:
    """publish() 전체 흐름용 가짜 에디터: 제목/본문/태그/완료 버튼 + 발행 레이어(FakeLayer)."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        layer: dict[str, Any] | None = None,
        owner_page_html: str | None = None,
        close_error: Exception | None = None,
        **page_flags: Any,
    ) -> None:
        self.draft = make_draft(tmp_path)
        self.publisher = make_publisher(tmp_path)
        self.page = FakePage()
        for name, value in page_flags.items():
            setattr(self.page, name, value)
        self.page.add_text("lmh 의 일상", FakeElement("blog-identity"))
        self.title = self.page.add(TITLE_SELECTOR, FakeElement("title"))
        self.page.title_element = self.title
        self.tags = self.page.add(TAG_SELECTOR, FakeElement("tags"))
        self.layer = FakeLayer(self.page, opened=False, **(layer or {"markup": "radio"}))
        self.complete = self.page.add(COMPLETE_SELECTOR, FakeElement("complete", text="완료", on_click=self.layer.open))
        self.page.content_by_url[self.layer.after_publish_url] = (
            owner_page_html if owner_page_html is not None else post_html()
        )
        self.runtime = FakeRuntime(self.page, close_error=close_error)
        install_runtime(monkeypatch, self.runtime)

    def run(self) -> PublishedPost:
        return self.publisher.publish(self.draft)


def expect_failure(callable_: Callable[[], Any], code: str) -> PublisherFailure:
    with pytest.raises(PublisherFailure) as raised:
        callable_()
    assert raised.value.code == code, f"{raised.value.code}: {raised.value}"
    return raised.value


# =================================================================================================================
# P1: 비공개가 증명되기 전에는 최종 버튼을 누르지 않는다
# =================================================================================================================
def test_private_radio_is_proven_then_only_a_non_public_final_button_is_clicked(tmp_path: Path) -> None:
    """비공개 선택 증명 + 공개 문구 검사를 없애면 공개 범위가 서버에 '20'(공개)으로 저장된다."""
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)

    publisher._publish_as_private(page, make_draft(tmp_path))

    assert layer.published == [("0", "비공개 저장")]
    assert len(layer.private.clicks) == 1
    assert layer.public.clicks == [] and layer.protected.clicks == []
    assert publisher._final_click_attempted is True


def test_force_click_is_not_used_for_private_option_or_final_button(tmp_path: Path) -> None:
    """force=True는 가려진/비활성 컨트롤도 눌러 선택 실패를 숨기므로 비공개 선택과 최종 클릭에서 쓰지 않는다."""
    page, layer = layer_page(markup="radio")

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    clicks = layer.private.clicks + layer.final.clicks
    assert clicks and all("force" not in kwargs for kwargs in clicks)


def test_missing_private_option_stops_with_ui_broken_and_never_clicks_a_final_button(tmp_path: Path) -> None:
    """비공개 옵션이 없는데(UI 개편) 최종 버튼을 누르면 기본값인 공개로 올라간다. 문구가 안전해 보여도 눌러선 안 된다."""
    page, layer = layer_page(markup="missing", final_label="저장")  # 버튼 문구는 '저장'이라 문구 검사만으로는 못 거른다
    decoy = layer.add_decoy_button(FINAL_SAVE_TEXT, "임시저장")
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "비공개 선택을 확인하지 못해" in str(failure)
    assert layer.published == []
    assert layer.final_clicks == 0 and decoy.clicks == []
    assert publisher._final_click_attempted is False


def test_hidden_private_radio_without_a_visible_label_is_never_clicked(tmp_path: Path) -> None:
    """숨겨진 라디오는 눌러도 선택을 증명할 수 없으므로 아예 누르지 않고 멈춘다(DOM상 앞에 있는 숨김 input이 가리던 사고)."""
    page, layer = layer_page(markup="hidden_input_label", final_label="저장")
    page.registry.pop(PRIVATE_LABEL)  # 눌러 볼 보이는 라벨이 없다
    publisher = make_publisher(tmp_path)

    expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert layer.private.clicks == []
    assert layer.published == [] and layer.final_clicks == 0


def test_hidden_radio_is_selected_through_its_visible_label_and_proven_via_the_radio(tmp_path: Path) -> None:
    """숨은 input + 보이는 label 패턴: 보이는 라벨을 누르고, 연결된 라디오의 선택 상태로 증명한다."""
    page, layer = layer_page(markup="hidden_input_label")

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert len(layer.label.clicks) == 1 and layer.private.clicks == []
    assert layer.published == [("0", "비공개 저장")]


def test_ineffective_private_click_stops_with_ui_broken_and_never_clicks_a_final_button(tmp_path: Path) -> None:
    """클릭이 먹지 않는데(라벨 기본동작 차단 등) 증명 없이 진행하면 기본값인 공개로 올라간다."""
    page, layer = layer_page(markup="hidden_input_label", click_effective=False, final_label="발행하기")
    layer.private.visible = lambda: True  # 라디오도 보이지만 눌러도 선택되지 않는다
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "클릭했지만 선택 상태를 확인하지 못함" in str(failure)
    assert layer.published == [] and layer.final_clicks == 0
    assert publisher._final_click_attempted is False


def test_private_click_errors_are_reported_not_swallowed(tmp_path: Path) -> None:
    """클릭 예외를 삼키던 try/except pass 대신, 후보별 실패 사유를 담아 UI_BROKEN으로 멈춘다."""
    page, layer = layer_page(markup="radio", final_label="저장")
    layer.private.click_error = PlaywrightError("Timeout 10000ms exceeded")
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "클릭/확인 실패" in str(failure)
    assert layer.published == [] and layer.final_clicks == 0


def test_other_visibility_still_selected_blocks_publishing(tmp_path: Path) -> None:
    """'비공개'로 보이는 컨트롤이 다른 그룹(예: value=0인 다른 설정)이라 공개가 그대로 선택돼 있으면 발행하지 않는다."""
    page, layer = layer_page(markup="radio", shared_group=False, final_label="저장")

    expect_failure(lambda: make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert layer.selected == "20"  # 공개가 여전히 선택돼 있었다
    assert layer.published == [] and layer.final_clicks == 0


def test_loose_value_zero_input_that_is_not_a_radio_is_ignored(tmp_path: Path) -> None:
    """input[value='0']가 라디오가 아닌 입력(숨김/텍스트 등)이면 누르지 않는다."""
    page = FakePage()
    layer = FakeLayer(page, markup="missing", final_label="저장")
    stray = page.add(PRIVATE_LOOSE, FakeElement("draft-flag", attrs={"type": "text", "value": "0"}))

    expect_failure(lambda: make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert stray.clicks == [] and layer.published == []


def test_custom_control_is_accepted_only_with_aria_evidence(tmp_path: Path) -> None:
    """input이 아닌 커스텀 컨트롤은 클릭 뒤 aria-checked="true"로 선택이 확인될 때만 인정한다."""
    page, layer = layer_page(markup="custom")

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert layer.published == [("0", "비공개 저장")]


def test_custom_control_whose_state_never_changes_is_rejected(tmp_path: Path) -> None:
    page, layer = layer_page(markup="custom", click_effective=False, final_label="저장")

    expect_failure(lambda: make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert layer.published == [] and layer.final_clicks == 0


@pytest.mark.parametrize(
    "attrs, expected",
    [
        ({"aria-checked": "true"}, True),
        ({"aria-pressed": "true"}, True),
        ({"class": "radio is-checked"}, True),
        ({"class": "opt active"}, True),
        ({"class": "selected"}, True),
        ({"class": "state_on"}, True),
        ({"class": "on"}, True),
        ({"aria-checked": "false", "class": "opt"}, False),
        ({"aria-pressed": "false"}, False),
        ({"class": "unchecked"}, False),
        ({"class": "inactive"}, False),
        ({"class": "not-selected"}, False),
        ({"class": "button radio-button option"}, False),  # 부분 문자열 'on'이 들어 있어도 인정하지 않는다
        ({}, False),
    ],
)
def test_selection_evidence_for_non_input_controls(attrs: dict[str, str], expected: bool) -> None:
    """클래스는 토큰 단위로만 인정한다('button'의 'on' 같은 부분 일치로 선택됨을 오판하지 않는다)."""
    control = FakeElement("custom", checked=None, attrs=attrs)

    assert (TistoryPublisher._selection_evidence(control) is not None) is expected


def test_is_checked_false_is_authoritative_even_if_class_says_active() -> None:
    """is_checked()가 지원되는 라디오가 '선택 안 됨'이면, 클래스에 active가 있어도 선택됨으로 보지 않는다."""
    radio = FakeElement("radio", checked=False, attrs={"class": "active", "aria-checked": "true"})

    assert TistoryPublisher._selection_evidence(radio) is None


def test_public_labelled_final_button_is_never_clicked(tmp_path: Path) -> None:
    """비공개가 증명돼도 공개 문구 버튼('공개 발행')만 있으면 누르지 않고 UI_BROKEN으로 멈춘다(문구가 안 바뀐 UI)."""
    page, layer = layer_page(markup="radio", final_label="공개 발행")  # '공개 발행' 후보 + .btn-primary 모두 공개 문구
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "안전한 최종 발행 버튼" in str(failure)
    assert layer.published == [] and layer.final_clicks == 0
    assert publisher._final_click_attempted is False


def test_public_labelled_candidate_is_skipped_and_a_later_safe_candidate_is_used(tmp_path: Path) -> None:
    """문구 검사는 후보별로 적용된다: '공개 저장' 버튼은 건너뛰고 뒤의 '비공개 저장' 버튼을 누른다."""
    page, layer = layer_page(markup="radio", final_label="공개 저장")
    safe = FakeElement("safe-final", text="비공개 저장", on_click=lambda: layer.published.append((layer.selected, "비공개 저장")))
    page.add(FINAL_LAYER_ID, safe)

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert layer.final.clicks == []  # '공개 저장'(has-text('저장'), .btn-primary로 잡힌 버튼)은 누르지 않았다
    assert len(safe.clicks) == 1
    assert layer.published == [("0", "비공개 저장")]


def test_draft_save_button_sharing_a_candidate_selector_is_not_used_as_the_final_button(tmp_path: Path) -> None:
    """has-text('저장')에는 에디터의 '임시저장'도 걸린다. DOM상 앞에 있어도 누르지 않고 진짜 최종 버튼을 누른다."""
    page, layer = layer_page(markup="radio")
    page.registry.pop(FINAL_PRIVATE_TEXT)  # 진짜 최종 버튼은 has-text('저장') 후보와 .btn-primary로만 닿는다
    draft_save = layer.add_decoy_button(FINAL_SAVE_TEXT, "임시저장", first=True)

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert draft_save.clicks == []
    assert len(layer.final.clicks) == 1
    assert layer.published == [("0", "비공개 저장")]


def test_only_a_draft_save_button_is_not_enough_to_publish(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio", final_label="공개 발행")
    draft_save = layer.add_decoy_button(FINAL_SAVE_TEXT, "임시저장")
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "임시저장 버튼" in str(failure)
    assert draft_save.clicks == [] and layer.published == [] and publisher._final_click_attempted is False


def test_every_visible_match_of_a_candidate_selector_is_considered(tmp_path: Path) -> None:
    """한 선택자에 보이는 버튼이 여럿일 때, 첫 번째가 공개 문구라고 거기서 멈추지 않고 다음 버튼을 본다."""
    page, layer = layer_page(markup="radio")
    page.registry.pop(FINAL_PRIMARY)  # 진짜 최종 버튼은 has-text('저장') 후보로만 닿는다
    page.registry.pop(FINAL_PRIVATE_TEXT)
    public_label = layer.add_decoy_button(FINAL_SAVE_TEXT, "공개 저장", first=True)
    hidden = layer.add_decoy_button(FINAL_SAVE_TEXT, "저장 숨김", first=True)
    hidden.visible = False
    # has-text('저장')의 DOM 순서: 숨김 버튼 → 공개 문구 버튼 → 진짜 '저장'. 앞의 두 버튼에서 멈추면 안 된다.
    assert [element.name for element, _ in page.registry[FINAL_SAVE_TEXT]] == ["decoy:저장 숨김", "decoy:공개 저장", "final-button"]

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert hidden.clicks == [] and public_label.clicks == []
    assert len(layer.final.clicks) == 1
    assert layer.published == [("0", "비공개 저장")]


@pytest.mark.parametrize(
    "label, expected",
    [("임시저장", True), ("임시 저장", True), ("저장", False), ("비공개 저장", False), ("발행하기", False), ("", False), (None, False)],
)
def test_draft_save_label_detection(label: str | None, expected: bool) -> None:
    assert tistory._is_draft_save_label(label) is expected


def test_removed_public_publish_candidate_is_not_in_the_candidate_list() -> None:
    """회귀 방지: '공개 발행' 후보를 다시 넣으면 문구 검사 하나에만 기대게 된다."""
    assert LEGACY_PUBLIC_BUTTON not in tistory.FINAL_BUTTON_SELECTORS
    assert tuple(tistory.FINAL_BUTTON_SELECTORS) == FINAL_CANDIDATES


@pytest.mark.parametrize(
    "label, expected",
    [
        ("공개 발행", True),
        ("공개발행", True),
        ("  공개\n저장 ", True),
        ("공개 / 비공개", True),  # 둘 다 적힌 모호한 문구는 공개 문구로 본다(안전 쪽)
        ("비공개 저장", False),
        ("비공개", False),
        ("발행하기", False),
        ("저장", False),
        ("", False),
        (None, False),
    ],
)
def test_public_label_detection(label: str | None, expected: bool) -> None:
    assert tistory._is_public_label(label) is expected


def test_publish_as_private_rejects_a_non_private_draft_before_touching_the_page(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")

    expect_failure(
        lambda: make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path, visibility="PUBLIC")),
        "PREFLIGHT_FAILED",
    )

    assert layer.private.clicks == [] and layer.published == []


def test_publish_rejects_non_private_draft_before_launching_a_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime(FakePage())
    install_runtime(monkeypatch, runtime)

    expect_failure(lambda: make_publisher(tmp_path).publish(make_draft(tmp_path, visibility="PUBLIC")), "PREFLIGHT_FAILED")

    assert runtime.launches == []


# =================================================================================================================
# P1 + P8 + P2 + P3: publish() 전체 흐름
# =================================================================================================================
def test_publish_happy_path_selects_private_publishes_and_returns_the_permalink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path, monkeypatch)

    post = scenario.run()

    assert post.url == POST_URL
    assert scenario.layer.published == [("0", "비공개 저장")]
    assert scenario.title.value == TITLE and scenario.title.fills == [TITLE]
    assert scenario.runtime.context.closed is True
    assert POST_URL in scenario.page.visited  # 소유자 세션으로 글을 열어 확인했다


def test_publish_stops_before_the_publish_layer_when_the_body_was_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P8: 본문 주입이 안 먹었는데 발행하면 빈 글이 VERIFIED가 됐다. 레이어를 열기 전에 UI_BROKEN으로 멈춘다."""
    scenario = Scenario(tmp_path, monkeypatch, inject_effective=False)

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "본문" in str(failure)
    assert scenario.complete.clicks == []  # 완료(레이어 열기)도 누르지 않았다
    assert scenario.layer.published == [] and scenario.layer.final_clicks == 0
    assert scenario.publisher._final_click_attempted is False


def test_public_labelled_button_is_not_clicked_even_to_open_the_publish_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """레이어를 여는 8단계 후보에 공개 문구('공개 발행') 버튼이 걸려도, 문구가 '완료'가 아니면 누르지 않는다."""
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.complete.text = "공개 발행"

    expect_failure(scenario.run, "UI_BROKEN")  # 누를 '완료' 버튼이 없고 레이어도 안 열려 있으니 9단계에서 비공개 옵션을 못 찾아 멈춘다

    assert scenario.complete.clicks == []
    assert scenario.layer.published == [] and scenario.layer.final_clicks == 0


def test_publish_stops_when_the_body_injection_script_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P8: 주입 스크립트 예외를 삼키던 except pass를 없앤 것."""
    scenario = Scenario(tmp_path, monkeypatch, inject_error=PlaywrightError("evaluation failed"))

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "본문 주입" in str(failure)
    assert scenario.layer.published == []


def test_publish_stops_when_readback_shows_the_title_was_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P8: 본문 주입이 제목 textarea까지 덮어쓰면(제목 자리에 본문 HTML) 발행하지 않는다."""
    scenario = Scenario(tmp_path, monkeypatch, clobber_title=True)

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "제목" in str(failure)
    assert scenario.complete.clicks == [] and scenario.layer.published == []


def test_publish_stops_when_the_title_cannot_be_read_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.title.fill_effective = False  # fill()이 먹지 않아 제목이 빈 채로 남는다

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "제목" in str(failure)
    assert scenario.layer.published == []


def test_body_readback_accepts_any_single_channel_but_requires_the_leading_words(tmp_path: Path) -> None:
    """CodeMirror/textarea/TinyMCE 중 하나에서라도 본문 앞 단어를 모두 읽을 수 있으면 주입된 것이다. 일부만 있으면 아니다."""
    injected = TistoryPublisher._body_was_injected
    assert injected({"codemirror": BODY_HTML, "textareas": [""], "tinymce": None}, BODY_HTML)
    assert injected({"codemirror": None, "textareas": ["", BODY_HTML], "tinymce": None}, BODY_HTML)
    assert injected({"codemirror": None, "textareas": [], "tinymce": "<h1>등본  저장</h1><p>확인할 내용입니다. 본문은 여러 단어로</p>"}, BODY_HTML)
    assert not injected({"codemirror": None, "textareas": [""], "tinymce": None}, BODY_HTML)
    assert not injected({"codemirror": "   ", "textareas": [], "tinymce": ""}, BODY_HTML)
    assert not injected({"codemirror": "<p>등본 저장</p>", "textareas": [], "tinymce": None}, BODY_HTML)  # 앞부분만 있다
    assert not injected(None, BODY_HTML)
    assert not injected("garbage", BODY_HTML)
    assert injected({"codemirror": "<img src='x.png'>", "textareas": [], "tinymce": None}, "<img src='x.png'>")  # 단어 없는 본문은 비어 있지 않음만 확인


def test_publish_without_a_private_option_ends_ui_broken_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 전체 흐름: 비공개 옵션이 없으면 최종 클릭 0회, 서버에는 아무 글도 생기지 않는다."""
    scenario = Scenario(tmp_path, monkeypatch, layer={"markup": "missing", "final_label": "저장"})

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "비공개 선택을 확인하지 못해" in str(failure)
    assert scenario.layer.published == [] and scenario.layer.final_clicks == 0
    assert scenario.runtime.context.closed is True
    assert scenario.publisher._final_click_attempted is False


@pytest.mark.parametrize(
    "landing_url",
    [
        "https://blog.tistory.com/manage/posts/",
        "https://blog.tistory.com/manage/newpost/?type=post",
        "https://blog.tistory.com/manage/entry/123",
        "https://blog.tistory.com/",
        "https://www.tistory.com/",
        "https://blog.tistory.com/category/생활",
    ],
)
def test_non_permalink_landing_page_is_not_accepted_as_the_post_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landing_url: str
) -> None:
    """P2: /manage/posts/ 같은 관리 화면 주소를 글 주소로 받아들이면 공개 글이 VERIFIED로 기록됐다."""
    scenario = Scenario(tmp_path, monkeypatch, layer={"markup": "radio", "after_publish_url": landing_url})

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert "글 주소" in str(failure)
    assert failure.post_url is None
    assert scenario.layer.published == [("0", "비공개 저장")]  # 클릭은 일어났으므로 글이 있을 수 있다고 알린다
    assert landing_url not in scenario.page.visited[1:]  # 소유자 검증으로 이어가지 않았다


def test_post_url_is_found_in_the_recent_post_list_when_the_page_is_a_manage_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """관리 목록에 머물면 제목이 같은 링크를 찾는다. 상대 링크(/123)는 현재 주소 기준으로 절대 주소로 만든다."""
    scenario = Scenario(
        tmp_path, monkeypatch, layer={"markup": "radio", "after_publish_url": "https://blog.tistory.com/manage/posts/"}
    )
    other = scenario.page.add("a", FakeElement("other-link", text="다른 글", attrs={"href": "/99"}))
    link = scenario.page.add("a", FakeElement("title-link", text=f"  {TITLE}\n", attrs={"href": "/123"}))
    scenario.page.content_by_url[POST_URL] = post_html()

    post = scenario.run()

    assert post.url == POST_URL
    assert other.clicks == [] and link.clicks == []
    assert POST_URL in scenario.page.visited


def test_manage_href_from_the_recent_post_list_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """목록의 링크가 편집 화면(/manage/post/123)이면 글 주소로 쓰지 않는다."""
    scenario = Scenario(
        tmp_path, monkeypatch, layer={"markup": "radio", "after_publish_url": "https://blog.tistory.com/manage/posts/"}
    )
    scenario.page.add("a", FakeElement("edit-link", text=TITLE, attrs={"href": "/manage/post/123"}))

    expect_failure(scenario.run, "PUBLISH_UNVERIFIED")


@pytest.mark.parametrize("phase", ["click-wait", "after-click-goto", "owner-check-goto"])
def test_any_exception_after_the_final_click_becomes_publish_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """P3: 클릭 뒤 예외를 UI_BROKEN으로 뭉개면 재시도가 중복 글을 만든다. 글이 있을 수 있으므로 PUBLISH_UNVERIFIED여야 한다."""
    scenario = Scenario(tmp_path, monkeypatch)
    if phase == "click-wait":
        # 최종 클릭 직후의 대기 중 브라우저가 닫히는 경우
        def boom() -> None:
            if scenario.layer.published:
                raise PlaywrightError("Target page, context or browser has been closed")

        scenario.page.on_wait = boom
    elif phase == "after-click-goto":
        scenario.page.goto_errors[POST_URL] = PlaywrightError("net::ERR_CONNECTION_RESET")
    else:
        scenario.page.goto_errors[POST_URL] = TimeoutError("navigation timeout")

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert "글이 이미 만들어졌을 수 있습니다" in str(failure)
    assert scenario.layer.published == [("0", "비공개 저장")]
    assert scenario.runtime.context.closed is True


def test_exception_after_click_keeps_the_known_post_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """글 주소를 알게 된 뒤의 예외에는 주소가 실려 워커가 result_url로 보존할 수 있다."""
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.page.goto_errors[POST_URL] = PlaywrightError("net::ERR_CONNECTION_RESET")

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert failure.post_url == POST_URL
    assert POST_URL in str(failure)


def test_owner_verification_failure_carries_the_post_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch, owner_page_html="<html><body>존재하지 않는 페이지</body></html>")

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert failure.post_url == POST_URL and POST_URL in str(failure)


def test_a_ui_broken_failure_after_the_click_is_reported_as_publish_unverified(tmp_path: Path) -> None:
    """클릭 이후에는 UI_BROKEN/AUTH_REQUIRED도 '글이 있을 수 있음'으로 바꿔 보고한다."""
    publisher = make_publisher(tmp_path)
    publisher._final_click_attempted = True

    converted = publisher._failure_after_attempt(PublisherFailure("UI_BROKEN", "x"), POST_URL)

    assert converted.code == "PUBLISH_UNVERIFIED" and converted.post_url == POST_URL


def test_exception_before_the_final_click_stays_ui_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.title.click_error = None
    scenario.page.registry.pop(TITLE_SELECTOR)  # 제목 입력창이 없다 → fill()에서 예외

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "required Tistory editor control was unavailable" in str(failure)
    assert scenario.layer.published == [] and scenario.publisher._final_click_attempted is False


def test_browser_cleanup_failure_does_not_mask_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """context.close()가 실패해도 이미 얻은 글 주소(성공)나 원래 예외를 덮어쓰지 않는다."""
    scenario = Scenario(tmp_path, monkeypatch, close_error=PlaywrightError("close failed"))

    assert scenario.run().url == POST_URL


# =================================================================================================================
# P2: 소유자 세션 검증은 제목 AND 본문
# =================================================================================================================
def owner_check(tmp_path: Path, content: str, *, url: str = POST_URL, status: int = 200, final_url: str | None = None, **draft_overrides: Any) -> None:
    page = FakePage()
    page.content_by_url[final_url or url] = content
    page.status_by_url[url] = status
    if final_url:
        page.redirects[url] = final_url
    TistoryPublisher._verify_owner_content(page, url, make_draft(tmp_path, **draft_overrides))


def test_owner_check_passes_when_title_and_body_are_present(tmp_path: Path) -> None:
    owner_check(tmp_path, post_html())


def test_owner_check_tolerates_entities_and_whitespace_differences(tmp_path: Path) -> None:
    """제목의 &,<,>가 이스케이프돼 있고 줄바꿈이 섞여도 올바른 글은 통과해야 한다."""
    title = "연말정산 & 환급 'FAQ' <총정리>"
    body = "<p>연말정산  환급은\n홈택스에서 확인합니다.</p>"
    page_html = (
        "<html><head><title>연말정산 &amp; 환급 &#39;FAQ&#39; &lt;총정리&gt; :: 블로그</title></head>"
        "<body><h1>연말정산 &amp;\n환급 &#x27;FAQ&#x27; &lt;총정리&gt;</h1><p>연말정산 환급은 홈택스에서&nbsp;확인합니다.</p></body></html>"
    )

    owner_check(tmp_path, page_html, title=title, body_html=body)


def test_owner_check_requires_the_title_not_just_the_body(tmp_path: Path) -> None:
    """이전에는 본문 단어 하나만 맞아도(또는 URL에 숫자만 있어도) 통과했다. 제목이 없으면 실패해야 한다."""
    content = f"<html><head><title>다른 제목</title></head><body>{BODY_HTML}</body></html>"

    expect_failure(lambda: owner_check(tmp_path, content), "PUBLISH_UNVERIFIED")


def test_owner_check_requires_the_body_not_just_the_title(tmp_path: Path) -> None:
    """글 관리 목록처럼 제목만 보이는 페이지는 통과하면 안 된다(빈 글이 VERIFIED가 되던 경로)."""
    content = f"<html><body><ul><li><a href='/1'>{html.escape(TITLE)}</a></li></ul></body></html>"

    failure = expect_failure(lambda: owner_check(tmp_path, content), "PUBLISH_UNVERIFIED")

    assert "본문" in str(failure) and failure.post_url == POST_URL


def test_owner_check_does_not_pass_on_the_first_title_word_alone(tmp_path: Path) -> None:
    """제목 첫 단어('정부24')만 같은 다른 글에는 속지 않는다(본문 단어가 다 있어도 제목 전체가 있어야 한다)."""
    content = f"<html><head><title>정부24 신청 방법</title></head><body>{BODY_HTML}</body></html>"

    failure = expect_failure(lambda: owner_check(tmp_path, content), "PUBLISH_UNVERIFIED")

    assert "제목" in str(failure) and "본문" not in str(failure).split("없는 항목:")[1]


def test_owner_check_does_not_trust_a_url_that_merely_looks_like_a_post(tmp_path: Path) -> None:
    """'entry'나 숫자가 든 주소 모양만으로는 통과하지 않는다: 페이지 내용이 맞아야 한다."""
    expect_failure(
        lambda: owner_check(tmp_path, "<html><body>존재하지 않는 페이지입니다</body></html>", url="https://blog.tistory.com/entry/missing"),
        "PUBLISH_UNVERIFIED",
    )


def test_owner_check_requires_all_leading_body_words(tmp_path: Path) -> None:
    """본문 앞 단어 5개 중 하나만 맞는('저장' 같은 흔한 단어) 페이지는 통과하지 않는다."""
    content = f"<html><head><title>{TITLE}</title></head><body><p>저장 버튼이 있습니다</p></body></html>"

    expect_failure(lambda: owner_check(tmp_path, content), "PUBLISH_UNVERIFIED")


def test_owner_check_fails_when_there_is_no_body_evidence_to_compare(tmp_path: Path) -> None:
    """대조할 단어가 없는 본문(이미지뿐)은 본문 증거를 만들 수 없으므로 통과시키지 않는다."""
    expect_failure(
        lambda: owner_check(tmp_path, post_html(body="<img src='a.png'>"), body_html="<img src='a.png'>"),
        "PUBLISH_UNVERIFIED",
    )


def test_owner_check_rejects_http_errors_and_manage_redirects(tmp_path: Path) -> None:
    expect_failure(lambda: owner_check(tmp_path, post_html(), status=404), "PUBLISH_UNVERIFIED")
    failure = expect_failure(
        lambda: owner_check(tmp_path, post_html(), final_url="https://blog.tistory.com/manage/posts/"), "PUBLISH_UNVERIFIED"
    )
    assert "관리 화면" in str(failure)


# =================================================================================================================
# P2: 익명 세션 검증은 '읽을 수 없다'는 양의 증거를 요구한다
# =================================================================================================================
def anonymous_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str = "<html><body></body></html>",
    status: int = 200,
    url: str = POST_URL,
    final_url: str | None = None,
    marker: bool = False,
    draft: PublicationDraft | None = None,
) -> str:
    anon = FakePage()
    anon.content_by_url[final_url or url] = content
    anon.status_by_url[url] = status
    if final_url:
        anon.redirects[url] = final_url
    anon.add_text("비공개", FakeElement("private-marker")) if marker else None
    runtime = FakeRuntime(anonymous_page=anon)
    install_runtime(monkeypatch, runtime)
    publisher = make_publisher(tmp_path)
    result = publisher.verify_private(PublishedPost(url), draft or make_draft(tmp_path))
    assert runtime.browser.closed is True
    return result


def test_anonymous_check_without_positive_evidence_is_unverified_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'제목이 안 보인다'만으로 통과하던 것을 막는다: 200 OK + 같은 주소 + 빈 화면(캡차 등)은 증명이 아니다."""
    failure = expect_failure(lambda: anonymous_check(tmp_path, monkeypatch), "PUBLISH_UNVERIFIED")

    assert "증거" in str(failure) and failure.post_url == POST_URL


@pytest.mark.parametrize("status", [401, 403, 404])
def test_anonymous_http_denial_is_positive_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    assert anonymous_check(tmp_path, monkeypatch, status=status) == f"HTTP {status}"


@pytest.mark.parametrize("status", [200, 301, 429, 500, 503])
def test_other_http_statuses_are_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, status=status), "PUBLISH_UNVERIFIED")


def test_redirect_to_a_login_page_is_positive_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    login = "https://www.tistory.com/auth/login?redirectUrl=https%3A%2F%2Fblog.tistory.com%2F123"

    evidence = anonymous_check(tmp_path, monkeypatch, final_url=login, content="<html><body>로그인</body></html>")

    assert evidence.startswith("redirected to https://www.tistory.com/auth/login")


def test_private_marker_text_is_positive_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert anonymous_check(tmp_path, monkeypatch, marker=True) == "private marker"


def test_visible_title_means_public_even_when_it_is_html_escaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """이전 비교는 이스케이프되지 않은 제목만 찾아서, &·<·> 가 든 제목의 공개 글이 '비공개'로 통과했다."""
    draft = make_draft(tmp_path, title="연말정산 & 환급 'FAQ' <총정리>")
    content = post_html(title=draft.title)

    failure = expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, content=content, draft=draft), "PUBLISH_UNVERIFIED")

    assert "공개 글일 수 있습니다" in str(failure)


def test_visible_body_alone_means_public(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = f"<html><head><title>다른 이름</title></head><body>{BODY_HTML}</body></html>"

    expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, content=content), "PUBLISH_UNVERIFIED")


def test_a_slug_containing_manage_does_not_skip_the_public_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """이전에는 URL에 'manage'라는 부분 문자열만 있어도(/entry/how-to-manage-money) 공개 검사를 건너뛰었다."""
    url = "https://blog.tistory.com/entry/how-to-manage-money"

    failure = expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, url=url, content=post_html()), "PUBLISH_UNVERIFIED")

    assert "공개 글일 수 있습니다" in str(failure)


def test_only_a_manage_path_prefix_skips_the_public_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """관리 화면(/manage…)으로 옮겨진 익명 방문은 글 페이지가 아니므로, 제목이 보여도 '이동' 증거로 본다."""
    evidence = anonymous_check(
        tmp_path, monkeypatch, final_url="https://blog.tistory.com/manage/posts/", content=post_html()
    )

    assert evidence == "redirected to https://blog.tistory.com/manage/posts/"


def test_permalink_to_permalink_redirect_is_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """공개 글이 /123 → /entry/슬러그로 정규화 리다이렉트되는 것을 '비공개 증거'로 오판하지 않는다."""
    canonical = "https://blog.tistory.com/entry/gov24-guide"

    expect_failure(
        lambda: anonymous_check(tmp_path, monkeypatch, final_url=canonical, content="<html><body>x</body></html>"),
        "PUBLISH_UNVERIFIED",
    )
    expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, final_url=canonical, content=post_html()), "PUBLISH_UNVERIFIED")


@pytest.mark.parametrize("final_url", ["about:blank", "chrome-error://chromewebdata/", ""])
def test_a_non_web_final_page_is_not_a_redirect_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_url: str) -> None:
    """응답이 없어 about:blank/오류 페이지에 머문 방문을 '다른 곳으로 이동했다'로 오판하지 않는다."""
    expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, final_url=final_url), "PUBLISH_UNVERIFIED")


def test_same_path_redirects_such_as_https_or_custom_domain_are_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expect_failure(
        lambda: anonymous_check(tmp_path, monkeypatch, final_url="https://www.my-blog.example/123"), "PUBLISH_UNVERIFIED"
    )


def test_a_title_echoing_login_wall_is_treated_as_not_proven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """보수적 선택: 관리 화면이 아닌 곳에서 제목이 보이면(로그인 안내가 제목을 되풀이하는 경우 포함) 상태 코드와 무관하게 실패시킨다."""
    expect_failure(lambda: anonymous_check(tmp_path, monkeypatch, status=403, content=post_html()), "PUBLISH_UNVERIFIED")


def test_verify_private_rejects_non_permalink_urls_without_opening_a_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime(anonymous_page=FakePage())
    install_runtime(monkeypatch, runtime)

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost("https://blog.tistory.com/manage/posts/"), make_draft(tmp_path)),
        "PUBLISH_UNVERIFIED",
    )

    assert runtime.launches == [] and failure.post_url == "https://blog.tistory.com/manage/posts/"


def test_verify_private_navigation_errors_become_unverified_with_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anon = FakePage()
    anon.goto_errors[POST_URL] = PlaywrightError("net::ERR_NAME_NOT_RESOLVED")
    runtime = FakeRuntime(anonymous_page=anon)
    install_runtime(monkeypatch, runtime)

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert failure.post_url == POST_URL and runtime.browser.closed is True


# =================================================================================================================
# 주소/문자열 도우미
# =================================================================================================================
@pytest.mark.parametrize(
    "url",
    [
        "https://lmh-life.tistory.com/123",
        "https://lmh-life.tistory.com/123/",
        "https://lmh-life.tistory.com/entry/some-slug",
        "https://lmh-life.tistory.com/entry/%ED%95%9C%EA%B8%80-%EC%A0%9C%EB%AA%A9",
        "https://lmh-life.tistory.com/entry/한글-제목",
        "http://lmh-life.tistory.com/45?category=1",
        "https://www.my-blog.example/7",  # 커스텀 도메인
    ],
)
def test_permalink_shapes_are_accepted(url: str) -> None:
    assert tistory._permalink_or_none(url) == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "/123",
        "about:blank",
        "ftp://lmh-life.tistory.com/123",
        "https://lmh-life.tistory.com",
        "https://lmh-life.tistory.com/",
        "https://lmh-life.tistory.com/manage/posts/",
        "https://lmh-life.tistory.com/manage/newpost/?type=post&returnURL=%2Fmanage%2Fposts%2F",
        "https://lmh-life.tistory.com/manage/entry/123",
        "https://lmh-life.tistory.com/manage",
        "https://lmh-life.tistory.com/category/생활",
        "https://lmh-life.tistory.com/entry/",
        "https://lmh-life.tistory.com/12ab",
        "https://lmh-life.tistory.com/123/456",
        "https://www.tistory.com/",
        "https://www.tistory.com/123",
        "https://tistory.com/entry/x",
    ],
)
def test_non_permalink_shapes_are_rejected(url: str | None) -> None:
    assert tistory._permalink_or_none(url) is None


def test_recent_post_lookup_ignores_whitespace_differences(tmp_path: Path) -> None:
    """목록 링크의 줄바꿈·연속 공백이 달라도 같은 제목이면 찾고, 제목이 다른 글(수정본 등)은 고르지 않는다."""
    page = FakePage()
    page.add("a", FakeElement("x", text="정부24 등본 저장 방법 (수정)", attrs={"href": "/1"}))
    page.add("a", FakeElement("y", text="  정부24  등본\n저장 방법 ", attrs={"href": "/2"}))

    assert make_publisher(tmp_path)._find_recent_post(page, TITLE) == "/2"
    assert make_publisher(tmp_path)._find_recent_post(page, "없는 제목") is None


def test_failure_after_attempt_attaches_the_known_post_url_to_an_unverified_failure(tmp_path: Path) -> None:
    """글 주소를 알게 된 뒤의 PUBLISH_UNVERIFIED에 주소가 실려 있지 않아도(다른 경로에서 올라온 실패) 채워서 돌려준다."""
    publisher = make_publisher(tmp_path)
    original = PublisherFailure("PUBLISH_UNVERIFIED", "x")

    converted = publisher._failure_after_attempt(original, POST_URL)

    assert converted is original and converted.post_url == POST_URL
    assert publisher._failure_after_attempt(PublisherFailure("UI_BROKEN", "y"), None).post_url is None


# =================================================================================================================
# 2차 독립 리뷰 반영: 발행 레이어 열기(완료) 클릭, 최종 문구의 양의 증거, 클릭 직전 재확인, 대화상자
# =================================================================================================================
class FakeDialog:
    def __init__(self, message: str, kind: str = "confirm") -> None:
        self.message = message
        self.type = kind
        self.accepted = False
        self.dismissed = False

    def accept(self) -> None:
        self.accepted = True

    def dismiss(self) -> None:
        self.dismissed = True


def test_layer_opener_candidates_do_not_include_buttons_that_can_publish() -> None:
    """회귀 방지: #publish-btn 과 '발행' 문구 후보로 레이어를 열면, 그 버튼이 최종 발행 버튼일 때 기본 공개 범위로 글이 올라간다."""
    assert tuple(tistory.LAYER_OPENER_SELECTORS) == (COMPLETE_ID, COMPLETE_SELECTOR)
    for selector in tistory.LAYER_OPENER_SELECTORS:
        assert "#publish-btn" not in selector and "발행" not in selector


@pytest.mark.parametrize(
    "label, expected",
    [("완료", True), ("  완 료 ", True), ("완료\n", True), ("발행 완료", False), ("작성 완료", False), ("공개 발행", False),
     ("완료하기", False), ("발행", False), ("", False), (None, False)],
)
def test_layer_opener_label_is_exactly_complete(label: str | None, expected: bool) -> None:
    assert tistory._is_layer_opener_label(label) is expected


def test_a_neutral_publish_button_is_never_used_to_open_the_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S8: 첫 번째로 보이는 '발행' 버튼이 즉시 글을 올리는 UI. 열기 후보에 걸려도 문구가 '완료'가 아니면 누르지 않는다."""
    scenario = Scenario(tmp_path, monkeypatch)
    neutral = FakeElement("neutral-publish", text="발행", on_click=lambda: scenario.layer.published.append(("20", "발행")))
    scenario.page.registry[COMPLETE_SELECTOR].insert(0, (neutral, None))  # DOM 순서상 '완료'보다 앞에 있다
    scenario.page.add(COMPLETE_ID, FakeElement("publish-btn-as-final", text="발행하기", on_click=neutral.on_click))

    post = scenario.run()

    assert neutral.clicks == [] and len(scenario.complete.clicks) == 1
    assert scenario.layer.published == [("0", "비공개 저장")]
    assert post.url == POST_URL


def test_without_a_complete_button_nothing_is_clicked_and_the_result_is_ui_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.page.registry.pop(COMPLETE_SELECTOR)

    failure = expect_failure(scenario.run, "UI_BROKEN")

    assert "비공개 선택을 확인하지 못해" in str(failure)
    assert scenario.layer.published == [] and scenario.layer.final_clicks == 0
    assert scenario.publisher._final_click_attempted is False


def test_complete_click_that_leaves_the_editor_without_a_layer_is_unverified_with_the_permalink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8 후속: '완료' 클릭이 곧바로 글을 올리고 글 페이지로 이동했다. 레이어가 안 열렸으니 UI_BROKEN(글 없음)이 아니라 PUBLISH_UNVERIFIED여야 한다."""
    scenario = Scenario(tmp_path, monkeypatch)

    def publishes_immediately() -> None:
        scenario.layer.published.append(("20", "완료"))
        scenario.page.url = POST_URL

    scenario.complete.on_click = publishes_immediately

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert scenario.publisher._final_click_attempted is True
    assert "발행 레이어" in str(failure) and "글이 이미 만들어졌을 수 있습니다" in str(failure)
    assert failure.post_url == POST_URL  # 현재 화면 주소가 글 주소면 건져서 실어 보낸다
    assert scenario.layer.final_clicks == 0


def test_complete_click_that_does_not_open_any_layer_is_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.complete.on_click = None  # 아무 일도 일어나지 않는다: 화면에는 레이어도 글 페이지도 없다

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert scenario.publisher._final_click_attempted is True and failure.post_url is None
    assert len(scenario.page.waits) >= tistory.LAYER_OPEN_WAIT_MS // tistory.LAYER_POLL_MS  # 레이어가 열리기를 기다렸다


def test_complete_click_error_is_unverified_because_the_click_may_have_been_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.complete.click_error = PlaywrightError("Timeout 10000ms exceeded while waiting for navigation")

    expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert scenario.publisher._final_click_attempted is True


def test_opening_the_layer_uses_a_bounded_click_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)

    scenario.run()

    assert scenario.complete.clicks[0].get("timeout") == tistory.CLICK_TIMEOUT_MS


def test_layer_without_a_private_option_is_ui_broken_and_not_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """레이어가 열려 있고(공개/보호 옵션이 보인다) 비공개만 없다면 '완료' 클릭이 글을 올리지 않았음이 분명하다 → 글 없음(UI_BROKEN)."""
    scenario = Scenario(tmp_path, monkeypatch, layer={"markup": "missing"})

    expect_failure(scenario.run, "UI_BROKEN")

    assert scenario.publisher._final_click_attempted is False
    assert scenario.layer.published == [] and scenario.layer.final_clicks == 0


@pytest.mark.parametrize("label", ["발행하기", "저장", "확인", "Publish", "등록"])
def test_a_neutral_final_label_is_never_clicked_even_when_the_proof_looks_fine(tmp_path: Path, label: str) -> None:
    """S2b/S9: 비공개 증명이 엉뚱한 컨트롤로 통과해도, 최종 버튼 문구에 '비공개'가 없으면 누르지 않는다(공개 글 방지)."""
    page, layer = layer_page(markup="radio", final_label=label)
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "비공개" in str(failure)
    assert layer.published == [] and layer.final_clicks == 0 and publisher._final_click_attempted is False


def test_a_neutral_candidate_is_skipped_and_a_later_private_labelled_button_is_used(tmp_path: Path) -> None:
    """문구 검사는 후보별로 건너뛰기여야 한다: 앞에 있는 '발행하기' 버튼에서 멈추지 않고 뒤의 '비공개 저장' 버튼을 누른다."""
    page, layer = layer_page(markup="radio")
    neutral = FakeElement("neutral-first", text="발행하기", on_click=lambda: layer.published.append((layer.selected, "발행하기")))
    page.registry[FINAL_PRIVATE_TEXT].insert(0, (neutral, None))  # DOM 순서상 진짜 버튼보다 앞에 있다

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert neutral.clicks == []
    assert layer.published == [("0", "비공개 저장")]


def test_unrelated_value_zero_radio_cannot_prove_private(tmp_path: Path) -> None:
    """S2b: 보이는 라디오 중 value=0인 것이 '댓글 불허' 같은 다른 설정이고 비공개 옵션은 사라진 UI. 문구가 중립이면 발행하지 않는다."""
    page, layer = layer_page(markup="missing", default="none", final_label="발행하기")
    comment_off = {"on": False}
    page.add(
        PRIVATE_RADIO,
        FakeElement(
            "comment-radio", attrs={"type": "radio", "value": "0"}, checked=lambda: comment_off["on"],
            on_click=lambda: comment_off.update(on=True),
        ),
    )
    publisher = make_publisher(tmp_path)

    expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert comment_off["on"] is True  # 라디오는 눌렸고 선택으로 읽혔지만
    assert layer.published == [] and layer.final_clicks == 0


def test_a_heading_with_an_active_class_cannot_prove_private(tmp_path: Path) -> None:
    """S9: 컨트롤이 아닌 제목 요소에 active 클래스가 있어 클래스 폴백이 통과해도, 문구가 중립이면 발행하지 않는다."""
    page, layer = layer_page(markup="missing", default="none", final_label="발행하기")
    page.add(PRIVATE_ID, FakeElement("heading", checked=None, attrs={"class": "title active"}))
    publisher = make_publisher(tmp_path)

    expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert layer.published == [] and layer.final_clicks == 0


@pytest.mark.parametrize(
    "label, expected",
    [
        ("비공개 저장", True), ("비공개", True), ("  비공개\n발행 ", True), ("비공개 발행", True),
        ("발행하기", False), ("저장", False), ("공개 발행", False), ("공개 저장", False),
        ("공개 / 비공개", False),  # 둘 다 적힌 모호한 문구는 공개 문구로 본다
        ("비공개 임시저장", False), ("임시저장", False), ("", False), (None, False),
    ],
)
def test_private_final_label_needs_positive_evidence(label: str | None, expected: bool) -> None:
    assert tistory._is_private_final_label(label) is expected


def test_selection_that_flips_back_to_public_before_the_final_click_is_caught_by_the_recheck(tmp_path: Path) -> None:
    """S6: 비공개를 증명한 뒤 레이어가 기본값(공개)으로 되돌려졌다. 최종 클릭 직전 재확인이 없으면 공개 글이 올라간다."""
    page, layer = layer_page(markup="radio")
    reads = {"n": 0}
    original = layer.private.checked

    def checked() -> bool:
        reads["n"] += 1
        if reads["n"] >= 2:  # 1번째 = 선택 증명, 2번째 = 최종 클릭 직전 재확인. 그 사이에 공개로 되돌려졌다
            layer.selected = "20"
        return original()

    layer.private.checked = checked
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "직전" in str(failure)
    assert reads["n"] == 2
    assert layer.published == [] and layer.final_clicks == 0 and publisher._final_click_attempted is False


def test_another_visibility_switching_on_before_the_final_click_is_caught_by_the_recheck(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)
    real = publisher._checked_non_private_option
    calls = {"n": 0}

    def conflict_check_after_public_switched_on() -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1번째 = 선택 증명 직후, 2번째 = 최종 클릭 직전 재확인
            layer.public.checked = True  # 비공개 라디오는 여전히 선택돼 보이지만 공개 라디오도 켜졌다
        return real(page)

    publisher._checked_non_private_option = lambda page_: conflict_check_after_public_switched_on()  # type: ignore[method-assign]

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "직전" in str(failure) and calls["n"] == 2
    assert layer.published == [] and layer.final_clicks == 0


def test_final_button_label_is_read_again_right_before_the_click(tmp_path: Path) -> None:
    """레이어가 최종 버튼 문구만 '공개 발행'으로 바꿔 놓는 경우: 후보를 고를 때 읽은 문구가 아니라 클릭 직전 문구를 본다."""
    page, layer = layer_page(markup="radio")
    for selector in (FINAL_PRIVATE_TEXT, FINAL_PUBLISH_TEXT, FINAL_SAVE_TEXT, FINAL_PRIMARY):
        page.registry.pop(selector, None)
    reads = {"n": 0}

    def label() -> str:
        reads["n"] += 1
        return "비공개 저장" if reads["n"] == 1 else "공개 발행"  # 1번째 = 후보 선택, 2번째 = 클릭 직전 재확인

    button = FakeElement("late-flip", text=label, on_click=lambda: layer.published.append((layer.selected, "?")))
    page.add(FINAL_PRIVATE_TEXT, button)
    publisher = make_publisher(tmp_path)

    expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert reads["n"] == 2 and button.clicks == [] and layer.published == []


def test_recheck_read_errors_stop_the_flow_before_the_click(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)
    real = publisher._selection_evidence
    calls = {"n": 0}

    def evidence_then_fail(target: Any) -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PlaywrightError("Execution context was destroyed")
        return real(target)

    publisher._selection_evidence = evidence_then_fail  # type: ignore[method-assign]

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "다시 확인하지 못해" in str(failure) and layer.final_clicks == 0


def test_the_final_click_happens_only_after_the_recheck_and_sets_the_flag_first(tmp_path: Path) -> None:
    """M05b: 플래그가 클릭 뒤에 세워지면 클릭이 예외로 끝났을 때 UI_BROKEN(글 없음)으로 잘못 보고된다. 클릭 시점에 이미 True여야 한다."""
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)
    seen: dict[str, bool] = {}

    def observe() -> None:
        seen["flag_at_click"] = publisher._final_click_attempted
        layer.published.append((layer.selected, layer.final.inner_text()))

    layer.final.on_click = observe

    publisher._publish_as_private(page, make_draft(tmp_path))

    assert seen == {"flag_at_click": True}


@pytest.mark.parametrize(
    "error",
    [PlaywrightError("Timeout 10000ms exceeded"), PlaywrightError("Target page, context or browser has been closed")],
)
def test_final_click_that_raises_is_publish_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.layer.final.click_error = error  # 클릭은 나갔는데 Playwright가 실패(내비게이션·닫힘·시간 초과)를 보고한다

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert scenario.publisher._final_click_attempted is True
    assert "글이 이미 만들어졌을 수 있습니다" in str(failure)


# --- 최종 클릭 가드(클릭 이벤트 시점의 페이지 안 재확인) -----------------------------------------------------------
def test_click_guard_is_armed_after_the_recheck_and_right_before_the_click(tmp_path: Path) -> None:
    """파이썬 재확인과 클릭 사이(수십 ms)에 레이어가 공개로 되돌려지는 경우를 닫는 마지막 방어선. 설치는 클릭 바로 앞이어야 한다."""
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)
    order: list[str] = []
    real_recheck = publisher._recheck_before_final_click

    def recheck(*args: Any) -> None:
        order.append("recheck")
        real_recheck(*args)

    publisher._recheck_before_final_click = recheck  # type: ignore[method-assign]
    real_evaluate = layer.final.evaluate

    def evaluate(script: str, arg: Any = None, **kwargs: Any) -> Any:
        order.append("guard")
        return real_evaluate(script, arg, **kwargs)

    layer.final.evaluate = evaluate  # type: ignore[method-assign]
    layer.final.on_click = lambda: (order.append("click"), layer.published.append((layer.selected, layer.final.inner_text())))

    publisher._publish_as_private(page, make_draft(tmp_path))

    assert order == ["recheck", "guard", "click"]
    [(script, handle)] = layer.final.evaluations
    assert script == tistory._CLICK_GUARD_SCRIPT and handle is layer.private  # 증명한 컨트롤을 가드에 넘긴다
    # 레이어가 다시 그려져 버튼이 사라졌을 때 30초(Playwright 기본)를 기다리지 않는다
    assert layer.final.evaluate_kwargs == {"timeout": tistory.CLICK_TIMEOUT_MS}
    assert layer.private.handle_kwargs == {"timeout": tistory.CLICK_TIMEOUT_MS}


def test_the_click_guard_script_mirrors_the_python_proof() -> None:
    """가드 JS가 파이썬 판단과 같은 기준을 담고 있는지 구조만 확인한다(동작은 실제 Chromium 시나리오로 검증했다)."""
    script = tistory._CLICK_GUARD_SCRIPT

    for needle in ("'비공개'", "'공개'", "'임시'", "value='15'", "value='20'", "isConnected", "target.control", "stopImmediatePropagation",
                   "preventDefault", "'click'", "'mousedown'", "'pointerdown'", "aria-checked", "aria-pressed", "window.__tistoryGuardBlocked"):
        assert needle in script, needle
    assert "true" in script and "return privateNow()" in script  # 설치 직후의 상태를 돌려준다
    assert "__tistoryGuardBlocked" in tistory._GUARD_BLOCKED_SCRIPT


def test_a_failed_guard_installation_prevents_the_click(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")
    layer.final.evaluate_error = PlaywrightError("Execution context was destroyed")
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "가드를 설치하지 못해" in str(failure)
    assert layer.final.clicks == [] and layer.published == [] and publisher._final_click_attempted is False


@pytest.mark.parametrize("armed", [False, None, "true", 1, 0])
def test_the_guard_must_report_exactly_true_when_it_is_installed(tmp_path: Path, armed: Any) -> None:
    """설치하는 순간 이미 비공개가 아니라고 보고하면(또는 알 수 없는 값을 돌려주면) 누르지 않는다."""
    page, layer = layer_page(markup="radio")
    layer.final.evaluate_result = armed
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "비공개 상태가 아님" in str(failure)
    assert layer.final.clicks == [] and layer.published == [] and publisher._final_click_attempted is False


def test_a_click_cancelled_by_the_guard_is_reported_and_keeps_the_unverified_flag(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)

    def cancelled_by_the_guard() -> None:
        page.guard_blocked = 3  # pointerdown / mousedown / click 이 취소됐다. 페이지 핸들러는 실행되지 않았다

    layer.final.on_click = cancelled_by_the_guard

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "가드가 최종 클릭을 취소" in str(failure) and "3개" in str(failure)
    assert layer.published == [] and publisher._final_click_attempted is True  # 클릭은 나갔으니 보수적으로 확인을 요구한다


def test_a_guard_cancelled_click_ends_unverified_in_the_full_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.layer.final.on_click = lambda: setattr(scenario.page, "guard_blocked", 1)

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert "가드가 최종 클릭을 취소" in str(failure) and scenario.layer.published == []


@pytest.mark.parametrize("blocked", [0, True, False, "1", None, -1, 1.5])
def test_only_a_positive_integer_count_means_the_guard_blocked_the_click(tmp_path: Path, blocked: Any) -> None:
    page, layer = layer_page(markup="radio")
    layer.final.on_click = lambda: (setattr(page, "guard_blocked", blocked), layer.published.append(("0", "비공개 저장")))

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert layer.published == [("0", "비공개 저장")]


def test_an_unreadable_guard_counter_after_the_click_is_not_a_block(tmp_path: Path) -> None:
    """클릭이 먹혀 화면이 이동하면 window를 읽을 수 없다. 그건 가드가 막지 않았다는 뜻이다."""
    page, layer = layer_page(markup="radio")
    page.guard_blocked_error = PlaywrightError("Execution context was destroyed, most likely because of a navigation")

    make_publisher(tmp_path)._publish_as_private(page, make_draft(tmp_path))

    assert layer.published == [("0", "비공개 저장")]


# --- 대화상자 ---------------------------------------------------------------------------------------------------
def test_dialogs_are_logged_and_only_public_sounding_ones_are_dismissed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tistory, "log_event", lambda logger, event, **fields: events.append((event, fields)))
    publisher = make_publisher(tmp_path)

    harmless = FakeDialog("임시 저장된 글이 있습니다. 불러오시겠습니까?")
    private_ok = FakeDialog("비공개로 저장하시겠습니까?")
    leaving = FakeDialog("", kind="beforeunload")
    public = FakeDialog("이 글은 공개로 발행됩니다. 계속하시겠습니까?")
    for dialog in (harmless, private_ok, leaving, public):
        publisher._on_dialog(dialog)

    assert [d.accepted for d in (harmless, private_ok, leaving)] == [True, True, True]
    assert [d.dismissed for d in (harmless, private_ok, leaving)] == [False, False, False]
    assert public.dismissed is True and public.accepted is False
    assert publisher._public_dialogs == [public.message]
    assert [event for event, _ in events] == ["tistory_dialog"] * 4
    assert events[3][1]["handled"] == "dismissed" and events[0][1]["handled"] == "accepted"
    assert events[3][1]["message"] == public.message and events[2][1]["dialog_type"] == "beforeunload"


def test_a_failure_while_answering_a_dialog_is_logged_and_does_not_escape(tmp_path: Path) -> None:
    publisher = make_publisher(tmp_path)

    class Broken(FakeDialog):
        def accept(self) -> None:
            raise PlaywrightError("dialog already handled")

    publisher._on_dialog(Broken("다른 확인창"))  # 예외가 새어 나오면 Playwright 이벤트 처리가 끊긴다


def test_publish_registers_the_dialog_handler_and_a_public_confirm_stops_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: 최종 클릭이 '공개로 발행됩니다. 계속하시겠습니까?' 확인창을 띄운다. 무조건 승인하던 때는 공개 글이 올라갔다."""
    scenario = Scenario(tmp_path, monkeypatch)
    original_publish = scenario.layer._publish

    def publish_unless_cancelled() -> None:
        dialog = FakeDialog("이 글은 공개로 발행됩니다. 계속하시겠습니까?")
        scenario.page.handlers["dialog"](dialog)
        scenario.dialog = dialog  # type: ignore[attr-defined]
        if dialog.accepted:
            original_publish()

    scenario.layer.final.on_click = publish_unless_cancelled

    failure = expect_failure(scenario.run, "PUBLISH_UNVERIFIED")

    assert scenario.dialog.dismissed is True  # type: ignore[attr-defined]
    assert scenario.layer.published == []  # 취소했으니 서버에는 아무것도 올라가지 않았다
    assert scenario.publisher._final_click_attempted is True
    assert "대화상자" in str(failure)


def test_a_public_confirm_before_the_final_click_stops_with_ui_broken(tmp_path: Path) -> None:
    page, layer = layer_page(markup="radio")
    publisher = make_publisher(tmp_path)
    publisher._public_dialogs.append("공개 발행 확인")

    failure = expect_failure(lambda: publisher._publish_as_private(page, make_draft(tmp_path)), "UI_BROKEN")

    assert "대화상자" in str(failure)
    assert layer.final_clicks == 0 and publisher._final_click_attempted is False


def test_publish_resets_the_dialog_record_between_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.publisher._public_dialogs.append("지난번 실행의 공개 확인창")

    assert scenario.run().url == POST_URL


# =================================================================================================================
# 2차 독립 리뷰 반영: 브라우저 시작 실패, 글 주소 확정
# =================================================================================================================
def test_browser_launch_failure_is_reported_as_retryable_launch_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필이 다른 창에서 열려 있으면 launch_persistent_context가 실패한다. 글이 있을 수 없으므로 UNEXPECTED_ERROR/격리가 아니다."""
    runtime = FakeRuntime(FakePage(), persistent_error=PlaywrightError("Target page, context or browser has been closed"))
    install_runtime(monkeypatch, runtime)
    publisher = make_publisher(tmp_path)

    failure = expect_failure(lambda: publisher.publish(make_draft(tmp_path)), "BROWSER_LAUNCH_FAILED")

    assert failure.retryable is True and "프로필" in str(failure)
    assert publisher._final_click_attempted is False
    assert runtime.launches == ["persistent"]


def test_publish_waits_for_the_permalink_up_to_the_configured_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E2: 글 주소로의 이동이 8초 넘게 걸리는 경우를 위해 고정 대기 대신 퍼머링크가 될 때까지 기다린다."""
    scenario = Scenario(tmp_path, monkeypatch)

    scenario.run()

    assert scenario.page.url_waits == [tistory.POST_URL_WAIT_MS]


def test_a_permalink_wait_that_times_out_falls_back_to_the_recent_post_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = Scenario(
        tmp_path, monkeypatch, layer={"markup": "radio", "after_publish_url": "https://blog.tistory.com/manage/posts/"}
    )
    scenario.page.add("a", FakeElement("title-link", text=TITLE, attrs={"href": "/123"}))
    scenario.page.content_by_url[POST_URL] = post_html()

    assert scenario.run().url == POST_URL
    assert scenario.page.url_waits == [tistory.POST_URL_WAIT_MS]


@pytest.mark.parametrize(
    "hrefs, expected",
    [
        (["/7", "/8"], "/8"),
        (["/8", "/7"], "/8"),
        (["/9", "/10"], "/10"),  # 문자열 비교가 아니라 숫자 비교
        (["/8", "/8/"], "/8"),  # 같은 글이 두 번 걸려도(끝의 / 차이) 하나로 본다
        (["/entry/old-slug", "/8"], None),  # 슬러그 주소가 섞이면 어느 쪽이 최신인지 알 수 없다
        (["/entry/a", "/entry/b"], None),
        (["/entry/only-one"], "/entry/only-one"),
        (["/manage/post/8", "/7"], "/7"),  # 편집 화면 링크는 후보가 아니다
        (["/manage/post/8"], None),
        ([], None),
    ],
)
def test_recent_post_lookup_picks_the_newest_numbered_post_among_same_title_links(
    tmp_path: Path, hrefs: list[str], expected: str | None
) -> None:
    page = FakePage()
    for index, href in enumerate(hrefs):
        page.add("a", FakeElement(f"link-{index}", text=TITLE, attrs={"href": href}))

    assert make_publisher(tmp_path)._find_recent_post(page, TITLE) == expected


def test_recent_post_lookup_skips_links_without_an_href(tmp_path: Path) -> None:
    page = FakePage()
    page.add("a", FakeElement("no-href", text=TITLE, attrs={}))
    page.add("a", FakeElement("empty-href", text=TITLE, attrs={"href": ""}))
    page.add("a", FakeElement("real", text=TITLE, attrs={"href": "/5"}))

    assert make_publisher(tmp_path)._find_recent_post(page, TITLE) == "/5"


def test_an_older_post_with_the_same_title_is_not_verified_in_place_of_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2: 관리 목록의 맨 앞 링크가 예전 같은 제목 글(/7)이어도 새 글(/8)을 고른다."""
    new_post = "https://blog.tistory.com/8"
    scenario = Scenario(
        tmp_path, monkeypatch, layer={"markup": "radio", "after_publish_url": "https://blog.tistory.com/manage/posts/"}
    )
    scenario.page.add("a", FakeElement("old", text=TITLE, attrs={"href": "/7"}))
    scenario.page.add("a", FakeElement("new", text=TITLE, attrs={"href": "/8"}))
    scenario.page.content_by_url[new_post] = post_html()

    assert scenario.run().url == new_post
    assert "https://blog.tistory.com/7" not in scenario.page.visited


def test_salvaging_a_post_url_requires_a_final_click_and_a_permalink(tmp_path: Path) -> None:
    publisher = make_publisher(tmp_path)
    page = FakePage(url=POST_URL)

    assert publisher._salvage_post_url(page) is None  # 최종 클릭 전이면 이 주소는 글 주소가 아니다
    publisher._final_click_attempted = True
    assert publisher._salvage_post_url(page) == POST_URL
    assert publisher._salvage_post_url(FakePage(url="https://blog.tistory.com/manage/posts/")) is None
    assert publisher._salvage_post_url(None) is None


# =================================================================================================================
# 2차 독립 리뷰 반영: 익명 검증은 대조군·재확인·일반 UA를 요구한다
# =================================================================================================================
def test_anonymous_check_opens_the_blog_home_as_a_control_before_the_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anon = FakePage()
    anon.status_by_url[POST_URL] = 403
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path))

    assert anon.visited == ["https://blog.tistory.com/", POST_URL, POST_URL]  # 대조군 → 글 → (간격 뒤) 글


@pytest.mark.parametrize("control_status", [403, 404, 429, 500, 503])
def test_a_blocked_control_means_a_403_on_the_post_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control_status: int
) -> None:
    """E4: 봇 차단은 공개 글에도 403을 준다. 같은 브라우저로 블로그 홈도 못 열면 글에 대한 403/404는 '비공개' 증거가 아니다."""
    anon = FakePage()
    anon.status_by_url["https://blog.tistory.com/"] = control_status
    anon.status_by_url[POST_URL] = 403
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert "대조군" in str(failure) and failure.post_url == POST_URL
    assert anon.visited == ["https://blog.tistory.com/"]  # 글은 열어 보지도 않았다


def test_a_control_that_is_redirected_to_another_site_proves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E4: 모든 주소를 캡차 서버로 보내는 차단. 블로그 홈이 200으로 열려도 다른 사이트로 이동했다면 대조군이 아니다."""
    anon = FakePage()
    anon.redirects["https://blog.tistory.com/"] = "https://security.example.com/challenge"
    anon.status_by_url[POST_URL] = 403
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert "대조군" in str(failure)


@pytest.mark.parametrize("wall", ["https://blog.tistory.com/captcha", "https://blog.tistory.com/auth/login?redirectUrl=%2F"])
def test_a_control_redirected_to_another_path_of_the_same_site_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wall: str
) -> None:
    """E4: 사이트 전체를 캡차·로그인 페이지로 보내는 벽은 같은 호스트의 200 응답이다. 홈이 '/'에 머물러야 대조군이다."""
    anon = FakePage()
    anon.redirects["https://blog.tistory.com/"] = wall
    anon.redirects[POST_URL] = wall
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert "대조군" in str(failure) and anon.visited == ["https://blog.tistory.com/"]


@pytest.mark.parametrize("other_root", ["https://other-blog.tistory.com/", "https://security.example.com/", "https://blog.tistory.com.evil.example/"])
def test_a_control_redirected_to_the_root_of_another_site_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_root: str
) -> None:
    """홈이 다른 사이트의 '/'(200)로 옮겨져도 대조군이 아니다. 경로가 '/'라서 경로 검사만으로는 못 거른다."""
    anon = FakePage()
    anon.redirects["https://blog.tistory.com/"] = other_root
    anon.status_by_url[POST_URL] = 403
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert "대조군" in str(failure) and anon.visited == ["https://blog.tistory.com/"]


def test_a_control_on_www_or_a_custom_domain_alias_is_still_the_same_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://my-blog.example/123"
    anon = FakePage()
    anon.redirects["https://my-blog.example/"] = "https://www.my-blog.example/"
    anon.status_by_url[url] = 404
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    assert make_publisher(tmp_path).verify_private(PublishedPost(url), make_draft(tmp_path)) == "HTTP 404"


def test_the_post_must_show_evidence_on_both_visits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E5: CDN이 방금 만든 공개 글에 첫 요청만 404를 준 경우. 3초 뒤 다시 열었을 때 글이 보이면 비공개로 인정하지 않는다."""
    anon = FakePage()
    anon.status_sequences[POST_URL] = [404, 200]
    anon.content_sequences[POST_URL] = ["<html><body></body></html>", post_html()]
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert "공개 글일 수 있습니다" in str(failure)
    assert anon.visited == ["https://blog.tistory.com/", POST_URL, POST_URL]
    assert tistory.ANONYMOUS_RECHECK_DELAY_MS in anon.waits


def test_two_different_kinds_of_evidence_are_both_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anon = FakePage()
    anon.status_sequences[POST_URL] = [404, 403]
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    assert make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)) == "HTTP 404; HTTP 403"


def test_a_second_visit_without_any_evidence_is_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anon = FakePage()
    anon.status_sequences[POST_URL] = [403, 200]
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    expect_failure(lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED")


def test_the_anonymous_visit_waits_for_the_page_to_settle_and_ignores_a_network_idle_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E6: 클라이언트 렌더링 글이 그려지기 전에 판단하면 안 된다. networkidle 대기가 시간 초과여도 판단은 이어진다."""
    anon = FakePage()
    anon.status_by_url[POST_URL] = 403
    anon.load_state_error = PlaywrightError("Timeout 8000ms exceeded")
    install_runtime(monkeypatch, FakeRuntime(anonymous_page=anon))

    evidence = make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path))

    assert evidence == "HTTP 403"
    assert anon.load_state_waits == ["networkidle", "networkidle"]  # 글 방문 2회(대조군은 기다리지 않는다)
    assert anon.waits.count(tistory.ANONYMOUS_SETTLE_MS) == 2


def test_the_anonymous_browser_uses_a_regular_chrome_user_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """헤드리스 기본 UA('HeadlessChrome')는 봇 차단에 걸려 공개 글에도 403을 받는다."""
    anon = FakePage()
    anon.status_by_url[POST_URL] = 404
    runtime = FakeRuntime(anonymous_page=anon)
    install_runtime(monkeypatch, runtime)

    make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path))

    user_agent = runtime.browser.page_options[0]["user_agent"]
    assert "Chrome/151.0.7922.34" in user_agent and "Headless" not in user_agent and user_agent.startswith("Mozilla/5.0")


@pytest.mark.parametrize("version", [None, "", "not-a-version", "151; rm -rf", 151])
def test_the_user_agent_falls_back_to_a_fixed_version_when_the_browser_version_is_unusable(version: Any) -> None:
    agent = tistory._anonymous_user_agent(SimpleNamespace(version=version))

    assert f"Chrome/{tistory.ANONYMOUS_FALLBACK_CHROME_VERSION} " in agent


def test_verify_private_launch_failure_is_unverified_and_keeps_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E8/finding 2: launch()가 try 밖에 있어 날 오류가 UNEXPECTED_ERROR로 샜고, 이미 만들어진 글이 FAILED(재시도 가능)로 보였다."""
    runtime = FakeRuntime(anonymous_page=FakePage(), launch_error=OSError("chromium executable is missing"))
    install_runtime(monkeypatch, runtime)

    failure = expect_failure(
        lambda: make_publisher(tmp_path).verify_private(PublishedPost(POST_URL), make_draft(tmp_path)), "PUBLISH_UNVERIFIED"
    )

    assert failure.post_url == POST_URL and "chromium executable is missing" in str(failure)
    assert runtime.browser.closed is False  # 열린 브라우저가 없으니 닫을 것도 없다


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://lmh.tistory.com/123", "https://lmh.tistory.com/"),
        ("http://localhost:8123/entry/x", "http://localhost:8123/"),
        ("https://www.my-blog.example/7?x=1", "https://www.my-blog.example/"),
    ],
)
def test_site_root(url: str, expected: str) -> None:
    assert tistory._site_root(url) == expected


@pytest.mark.parametrize(
    "first, second, expected",
    [
        ("https://a.tistory.com/", "https://a.tistory.com/x", True),
        ("https://a.tistory.com/", "https://www.a.tistory.com/", True),
        ("https://A.tistory.com/", "https://a.tistory.com./", True),
        ("https://a.tistory.com/", "https://b.tistory.com/", False),
        ("https://a.tistory.com/", "https://security.example.com/", False),
        ("https://a.tistory.com/", "about:blank", False),
        ("about:blank", "about:blank", False),
        ("https://a.tistory.com/", "https://a.tistory.com.evil.example/", False),
    ],
)
def test_same_site(first: str, second: str, expected: bool) -> None:
    assert tistory._same_site(first, second) is expected


def test_class_tokens_that_merely_start_with_a_keyword_are_not_selected() -> None:
    """M29: 클래스 토큰 정규식의 끝 고정(`$`)이 빠지면 online/onboarding 같은 클래스가 '선택됨'으로 읽힌다."""
    for css in ("online", "onboarding", "activate", "selected-none", "checked-out"):
        assert tistory._class_says_selected(css) is False


# =================================================================================================================
# 워커 + 어댑터 통합 (가짜 Page): 상태 기록까지 확인
# =================================================================================================================
def make_worker_db(tmp_path: Path, scenario: Scenario) -> tuple[Any, int]:
    engine = create_engine(f"sqlite:///{tmp_path / 'integration.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        article = Article(content_hash="a" * 64, status=ArticleStatus.READY_TO_PUBLISH)
        session.add(article)
        session.flush()
        version = ArticleVersion(
            article_id=article.id,
            version_number=1,
            title=scenario.draft.title,
            body_html=scenario.draft.body_html,
            tags_json=scenario.draft.tags,
            category=scenario.draft.category,
            thumbnail_path=str(scenario.draft.thumbnail_path),
            content_hash="a" * 64,
            status=ArticleStatus.READY_TO_PUBLISH,
        )
        session.add(version)
        session.flush()
        publish = PublishJob(
            article_version_id=version.id,
            target_blog_name="lmh 의 일상",
            category=scenario.draft.category,
            visibility="PRIVATE",
            status=PublishStatus.PENDING,
        )
        session.add(publish)
        session.flush()
        job = Job(job_type="PUBLISH_TISTORY", entity_id=publish.id, payload_json={}, status=JobStatus.PENDING, priority=90, max_attempts=2)
        session.add(job)
        session.commit()
        return engine, job.id


def run_worker_with_adapter(tmp_path: Path, scenario: Scenario) -> tuple[Any, dict[str, Any]]:
    engine, job_id = make_worker_db(tmp_path, scenario)
    with Session(engine) as session:
        worker = PublisherWorker(session, make_settings(tmp_path), scenario.publisher)
        assert worker.run_once("integration") is True
    with Session(engine) as session:
        publish = session.scalar(select(PublishJob))
        state = {
            "job": session.get(Job, job_id),
            "publish": publish,
            "version": session.scalar(select(ArticleVersion)),
            "article": session.scalar(select(Article)),
        }
        session.expunge_all()
    engine.dispose()
    return engine, state


def test_worker_with_adapter_quarantines_and_publishes_nothing_when_private_is_unprovable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1+P5 통합: 비공개를 증명 못 하면 서버에 글이 없고, 상태는 UI_BROKEN + QUARANTINED로 남는다."""
    scenario = Scenario(tmp_path, monkeypatch, layer={"markup": "missing", "final_label": "저장"})

    _, state = run_worker_with_adapter(tmp_path, scenario)

    assert scenario.layer.published == []
    assert state["job"].status is JobStatus.FAILED and state["job"].last_error_code == "UI_BROKEN"
    assert state["publish"].status is PublishStatus.UI_BROKEN and state["publish"].result_url is None
    assert state["version"].status is ArticleStatus.QUARANTINED and state["article"].status is ArticleStatus.QUARANTINED


def test_worker_with_adapter_records_the_url_of_a_post_that_failed_owner_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3 통합: 소유자 검증이 실패해도(글은 이미 있다) 글 주소가 result_url과 실패 메시지에 남는다."""
    scenario = Scenario(tmp_path, monkeypatch, owner_page_html="<html><body>엉뚱한 페이지</body></html>")

    _, state = run_worker_with_adapter(tmp_path, scenario)

    assert scenario.layer.published == [("0", "비공개 저장")]
    assert state["publish"].status is PublishStatus.PUBLISH_UNVERIFIED
    assert state["publish"].result_url == POST_URL
    assert POST_URL in state["job"].last_error_message
    assert state["version"].status is ArticleStatus.QUARANTINED


def test_worker_with_adapter_exception_after_click_is_unverified_and_keeps_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.page.goto_errors[POST_URL] = PlaywrightError("net::ERR_CONNECTION_RESET")

    _, state = run_worker_with_adapter(tmp_path, scenario)

    assert state["job"].last_error_code == "PUBLISH_UNVERIFIED"
    assert state["publish"].status is PublishStatus.PUBLISH_UNVERIFIED and state["publish"].result_url == POST_URL
    assert state["article"].status is ArticleStatus.QUARANTINED


# =================================================================================================================
# P6: 썸네일 경로는 절대 경로로 저장한다
# =================================================================================================================
@pytest.mark.parametrize("kind", ["private", "draft"])
def test_registration_stores_absolute_thumbnail_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """상대 경로로 저장하면 다른 디렉터리에서 시작한 워커가 썸네일을 못 찾아 잡을 소진한다."""
    project = tmp_path / "project"
    (project / "storage").mkdir(parents=True)
    (project / "storage" / "lifestyle.png").write_bytes(b"png")
    monkeypatch.chdir(project)
    engine = create_engine(f"sqlite:///{tmp_path / 'registration.db'}")
    Base.metadata.create_all(engine)
    relative = Path("storage/lifestyle.png")

    with Session(engine) as session:
        if kind == "private":
            register_private_article(
                session,
                StaticArticleInput(title="제목", body_html="<p>본문</p>", tags=["a"], category="C", target_blog_name="B", thumbnail_path=relative),
            )
        else:
            register_draft_article(
                session, DraftArticleInput(title="제목", body_html="<p>본문</p>", tags=["a"], category="C", thumbnail_path=relative)
            )
        session.commit()
        stored_version = session.scalar(select(ArticleVersion.thumbnail_path))
        stored_asset = session.scalar(select(MediaAsset.local_path))

    expected = str((project / "storage" / "lifestyle.png").resolve())
    assert stored_version == expected and stored_asset == expected
    assert Path(stored_version).is_absolute()
    engine.dispose()


# =================================================================================================================
# P10: 20260730_02 downgrade는 MySQL에서 안전 검사를 먼저 한다
# =================================================================================================================
def load_migration_module() -> Any:
    path = REPO_ROOT / "migrations" / "versions" / "20260730_02_topic_candidates_and_drafts.py"
    spec = importlib.util.spec_from_file_location("migration_20260730_02", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOp:
    """alembic.op 대역: 호출 순서를 기록하고, SELECT COUNT 결과를 정해 준다."""

    def __init__(self, counts: dict[str, int], dialect: str = "mysql") -> None:
        self.calls: list[str] = []
        self.counts = counts
        self.dialect = dialect

    def get_bind(self) -> Any:
        recorder = self

        class Result:
            def __init__(self, value: int) -> None:
                self.value = value

            def scalar_one(self) -> int:
                return self.value

        class Bind:
            dialect = SimpleNamespace(name=recorder.dialect)

            def execute(self, statement: Any) -> Result:
                sql = str(statement)
                recorder.calls.append(f"check: {sql}")
                table = "article_versions" if "article_versions" in sql else "articles"
                return Result(recorder.counts.get(table, 0))

        return Bind()

    def drop_index(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("drop_index")

    def drop_table(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("drop_table")

    def execute(self, sql: str) -> None:
        self.calls.append(f"ddl: {sql}")


@pytest.mark.parametrize("counts", [{"articles": 2}, {"article_versions": 1}])
def test_mysql_downgrade_refuses_before_dropping_anything_when_drafts_exist(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> None:
    """MySQL DDL은 롤백되지 않는다. 검사 전에 테이블을 지우면 실패한 뒤에도 topic_candidates가 사라져 있다."""
    migration = load_migration_module()
    recorder = RecordingOp(counts)
    monkeypatch.setattr(migration, "op", recorder)

    with pytest.raises(RuntimeError, match="DRAFT"):
        migration.downgrade()

    assert not any(call in ("drop_index", "drop_table") or call.startswith("ddl:") for call in recorder.calls)


def test_mysql_downgrade_runs_the_check_first_then_drops_and_narrows_the_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = load_migration_module()
    recorder = RecordingOp({})
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    kinds = [call.split(":")[0] for call in recorder.calls]
    assert kinds[:2] == ["check", "check"]  # articles와 article_versions 모두 확인
    assert kinds[2:4] == ["drop_index", "drop_table"]
    assert kinds[4:] == ["ddl", "ddl"]
    assert all("'DRAFT'" not in call for call in recorder.calls if call.startswith("ddl:"))


def test_non_mysql_downgrade_just_drops_the_topic_table(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = load_migration_module()
    recorder = RecordingOp({"articles": 5}, dialect="sqlite")
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.calls == ["drop_index", "drop_table"]
