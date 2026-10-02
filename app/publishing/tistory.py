from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
import html
import logging
import re

import yaml
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Locator, Page, sync_playwright

from app.core.logging import log_event
from app.core.settings import Settings
from app.publishing.client import PublishedPost, PublisherFailure
from app.publishing.guards import PublicationDraft

logger = logging.getLogger("tistory_automation")

# ---------------------------------------------------------------------------------------------------------------
# 실제 Tistory 에디터로 검증되지 않은 가정들 (운영 투입 전 비공개 글 1건으로 수동 드라이런 필요)
#  A1. '완료'를 누르면 열리는 발행 레이어에 공개 범위 라디오가 있고 비공개=value 0, 보호=15, 공개=20 이다.
#  A2. 비공개 옵션은 보이는 라디오이거나, 숨겨진 라디오 + 보이는 <label>이다(라벨을 눌러도 라디오가 선택된다).
#  A3. 비공개를 고르면 최종 버튼 문구가 '비공개 …'(예: '비공개 저장')로 바뀐다. 문구에 '비공개'가 없거나 '공개'(비공개 제외)가
#      남은 버튼은 절대 누르지 않는다. 문구가 안 바뀌는 UI라면 UI_BROKEN으로 멈추므로 REQUIRED_FINAL_LABEL을 실제 문구에 맞춘다.
#  A4. 발행 직후 브라우저가 글 퍼머링크(/숫자 또는 /entry/슬러그)로 이동하거나(POST_URL_WAIT_MS까지 기다린다),
#      현재 화면에 제목이 같은 글 링크가 있다(여럿이면 글 번호가 가장 큰 것).
#  A5. 비로그인으로 비공개 글에 접근하면 HTTP 401/403/404가 오거나, 다른 경로(로그인 등)로 리다이렉트되거나,
#      private_marker 문구가 보인다. 이 셋 중 하나가 없으면 비공개로 인정하지 않는다.
#      그리고 같은 블로그의 홈(/)은 같은 익명 브라우저로 HTTP 200으로 열린다(대조군. 안 열리면 봇 차단·점검 등으로 보고 판단하지 않는다).
#  A6. HTML 모드 에디터는 CodeMirror(getValue) 또는 textarea/TinyMCE로 본문을 되읽을 수 있다.
#  A7. 에디터에는 '임시저장'(초안 저장) 버튼이 있고 has-text('저장') 후보에 함께 걸린다. 임시저장 버튼은 최종 버튼으로 누르지 않는다.
#  A8. Playwright의 is_checked()는 <label>을 그 label이 가리키는 라디오(for/중첩)의 상태로 읽는다(로컬 가짜 사이트의
#      Chromium으로만 확인). 비공개 증명의 근거는 로그(private_visibility_proven)와 verification_details.evidence에 남는다.
#  A9. 발행 레이어를 여는 버튼은 문구가 정확히 '완료'인 #publish-layer-btn 또는 button 이다. 최종 버튼일 수 있는 #publish-btn 이나
#      '발행' 문구 버튼은 열기 버튼으로 누르지 않는다.
#  A10. 발행 레이어가 열리면 비공개 옵션이 보인다. '완료'를 눌렀는데 옵션이 나타나지 않으면 그 클릭이 글을 올렸을 수 있으므로
#      PUBLISH_UNVERIFIED로 본다.
#  A11. 에디터가 띄우는 confirm/alert 중 '공개'(비공개 제외)를 말하는 것은 취소하고(발행 중단), 나머지는 승인한다. 모든 대화상자는 로그에 남는다.
#  A12. 최종 버튼은 pointer·mouse·click 이벤트로 발행한다. 클릭 가드(_CLICK_GUARD_SCRIPT)는 그 이벤트만 막으므로, 키보드 Enter 등으로만
#      발행되는 UI는 막지 못한다.
# ---------------------------------------------------------------------------------------------------------------

# 비공개 옵션 후보: 위에서부터 '보이는 첫 요소'를 눌러 보고, 선택 상태가 증명되는 첫 후보에서 멈춘다.
PRIVATE_OPTION_SELECTORS = (
    "input[type='radio'][value='0']",
    "#visibility-private",
    "label:has-text('비공개')",
    "input[value='0']",
)
# 비공개를 골랐다면 선택돼 있으면 안 되는 옵션(보호=15, 공개=20). 하나라도 선택돼 있으면 증명 실패로 본다.
NON_PRIVATE_OPTION_SELECTORS = (
    "input[type='radio'][value='15']",
    "input[type='radio'][value='20']",
)
# 최종 발행 버튼 후보. '공개 발행' 후보는 제거했고, 어떤 후보든 누르기 전에 문구를 읽어 '비공개'가 있고 공개·임시저장 문구가 아닐 때만 누른다.
FINAL_BUTTON_SELECTORS = (
    "button:has-text('비공개')",
    "button:has-text('발행하기')",
    "button:has-text('저장')",
    "#layer-publish-btn",
    ".btn_confirm",
    "button.btn-confirm",
    "button.btn-primary",
)
# 발행 레이어를 여는 '완료' 버튼 후보. 최종 발행 버튼이 될 수 있는 #publish-btn 과 '발행' 문구 후보는 넣지 않는다.
LAYER_OPENER_SELECTORS = (
    "#publish-layer-btn",
    "button:has-text('완료')",
)
LAYER_OPENER_LABEL = "완료"
# 최종 발행 버튼 문구에 반드시 들어 있어야 하는 말(비공개가 적용됐다는 양의 증거)
REQUIRED_FINAL_LABEL = "비공개"
CLICK_TIMEOUT_MS = 10_000
# '완료'를 누른 뒤 발행 레이어(= 비공개 옵션)가 나타나기를 기다리는 시간
LAYER_OPEN_WAIT_MS = 6_000
LAYER_POLL_MS = 500
# 발행 뒤 브라우저가 글 퍼머링크로 이동하기를 기다리는 최대 시간(이후에는 최근 글 목록에서 찾는다)
POST_URL_WAIT_MS = 20_000
# 익명 방문 검증: 페이지가 자리 잡기를 기다리는 시간과, 같은 판단을 한 번 더 하기 전의 간격
ANONYMOUS_NETWORK_IDLE_MS = 8_000
ANONYMOUS_SETTLE_MS = 1_500
ANONYMOUS_RECHECK_DELAY_MS = 3_000
ANONYMOUS_FALLBACK_CHROME_VERSION = "124.0.0.0"

_WHITESPACE = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")
# 글 퍼머링크 경로: /123 또는 /entry/슬러그 (끝의 / 허용)
_PERMALINK_PATH = re.compile(r"^/(?:\d+|entry/.+?)/?$")
_NUMERIC_PERMALINK_PATH = re.compile(r"^/(\d+)/?$")
# 비공개 옵션이 '선택됨'임을 말하는 클래스 토큰(unchecked/inactive/button 같은 부분 일치는 인정하지 않는다)
_SELECTED_CLASS_TOKEN = re.compile(r"^(?:is[-_]|state[-_])?(?:checked|active|selected|on)$")
_MAIN_SITE_HOSTS = frozenset({"tistory.com", "www.tistory.com"})
# 익명으로 글에 접근했을 때 '읽을 수 없음'을 뜻하는 HTTP 상태
_NOT_READABLE_STATUSES = (401, 403, 404)

# 본문 주입: CodeMirror → 모든 textarea(제목 제외) → TinyMCE 순으로 같은 HTML을 한 번씩 넣는다.
_INJECT_BODY_SCRIPT = """(htmlContent) => {
    const cmEl = document.querySelector('.CodeMirror');
    if (cmEl && cmEl.CodeMirror) {
        cmEl.CodeMirror.setValue(htmlContent);
        cmEl.CodeMirror.refresh();
        if (typeof cmEl.CodeMirror.save === 'function') {
            cmEl.CodeMirror.save(); // Form textarea 동기화!
        }
    }
    const textareas = document.querySelectorAll('textarea:not(#post-title-inp)');
    textareas.forEach(ta => {
        ta.value = htmlContent;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        ta.dispatchEvent(new Event('blur', { bubbles: true }));
    });
    if (window.tinymce && window.tinymce.activeEditor) {
        window.tinymce.activeEditor.setContent(htmlContent);
        window.tinymce.activeEditor.save();
    }
}"""

# 본문 되읽기(읽기 전용): 주입이 끝난 뒤의 에디터 값을 채널별로 돌려준다. 없는 채널은 null/빈 배열.
_READ_BODY_SCRIPT = """() => {
    const result = { codemirror: null, textareas: [], tinymce: null };
    const cmEl = document.querySelector('.CodeMirror');
    if (cmEl && cmEl.CodeMirror && typeof cmEl.CodeMirror.getValue === 'function') {
        result.codemirror = cmEl.CodeMirror.getValue();
    }
    document.querySelectorAll('textarea:not(#post-title-inp)').forEach(ta => result.textareas.push(ta.value));
    if (window.tinymce && window.tinymce.activeEditor) {
        result.tinymce = window.tinymce.activeEditor.getContent();
    }
    return result;
}"""


# 최종 버튼 클릭 가드. 클릭 이벤트가 페이지 핸들러에 닿기 직전(같은 JS 작업 안)에 비공개 상태를 다시 읽어, 아니면 이벤트를 취소한다.
# 파이썬에서의 재확인과 실제 클릭 사이(수십 ms)에 레이어가 공개로 되돌려지는 경우를 닫는다. 판단 기준은 파이썬 쪽과 같다:
# 버튼 문구에 '비공개'가 있고 '공개'(비공개 제외)·'임시'가 없으며, 보호/공개 라디오가 꺼져 있고, 증명한 컨트롤이 아직 선택돼 있다.
# 버튼이나 컨트롤이 DOM에서 교체돼 연결이 끊겼으면(재렌더링) 막는다. 설치 직후의 상태를 반환한다.
_CLICK_GUARD_SCRIPT = """(button, target) => {
    const squash = (text) => Array.from(text || '').filter((ch) => ch.trim() !== '').join('');
    const privateNow = () => {
        if (!button.isConnected || !target.isConnected) return false;
        const label = squash(button.innerText || button.textContent);
        if (!label.includes('비공개') || label.split('비공개').join('').includes('공개') || label.includes('임시')) return false;
        for (const other of document.querySelectorAll("input[type='radio'][value='15'], input[type='radio'][value='20']")) {
            if (other.checked) return false;
        }
        const control = target instanceof HTMLInputElement ? target : (target.control || null);
        if (control && (control.type === 'radio' || control.type === 'checkbox')) return control.checked === true;
        const tokens = (target.getAttribute('class') || '').toLowerCase().split(' ');
        return target.getAttribute('aria-checked') === 'true' || target.getAttribute('aria-pressed') === 'true'
            || tokens.some((token) => /^(?:is[-_]|state[-_])?(?:checked|active|selected|on)$/.test(token));
    };
    const guard = (event) => {
        if (button.isConnected && event.target !== button && !button.contains(event.target)) return;
        if (!privateNow()) {
            event.stopImmediatePropagation();
            event.preventDefault();
            window.__tistoryGuardBlocked = (window.__tistoryGuardBlocked || 0) + 1;
        }
    };
    window.__tistoryGuardBlocked = 0;
    for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        window.addEventListener(name, guard, true);
    }
    return privateNow();
}"""
# 가드가 취소한 이벤트 수(읽기 전용). 클릭이 먹혀 화면이 이동하면 읽을 수 없고, 그건 가드가 막지 않았다는 뜻이다.
_GUARD_BLOCKED_SCRIPT = "() => window.__tistoryGuardBlocked || 0"


def _normalize_text(value: str | None) -> str:
    """엔티티를 풀고 공백을 하나로 접어 비교한다(이스케이프·줄바꿈·공백 차이를 흡수)."""
    return _WHITESPACE.sub(" ", html.unescape(value or "")).strip().casefold()


def _page_texts(content: str) -> tuple[str, str]:
    """페이지 원문과, 태그를 걷어낸 텍스트(둘 다 정규화)."""
    return _normalize_text(content), _normalize_text(_TAGS.sub(" ", content or ""))


def _contains_text(content: str, needle: str) -> bool:
    target = _normalize_text(needle)
    return bool(target) and any(target in text for text in _page_texts(content))


def _leading_words(body_html: str, limit: int = 5) -> list[str]:
    """본문 앞쪽의 2자 이상 단어(태그 제거·엔티티 해제). 게시/에디터 내용 대조용."""
    text = html.unescape(_TAGS.sub(" ", body_html or ""))
    return [word for word in text.split() if len(word) >= 2][:limit]


def _contains_words(content: str, words: list[str]) -> bool:
    """words 전부가 content에 있어야 한다(하나라도 있으면 통과하던 느슨한 OR를 쓰지 않는다)."""
    if not words:
        return False
    texts = _page_texts(content)
    return all(any(_normalize_text(word) in text for text in texts) for word in words)


def _is_manage_path(path: str) -> bool:
    return (path or "").startswith("/manage")


def _permalink_or_none(url: str | None) -> str | None:
    """글 퍼머링크처럼 생긴 주소만 돌려준다. /manage… 화면, 티스토리 메인, 목록/에디터 주소는 None.

    호스트는 커스텀 도메인 때문에 제한하지 않는다(http/https + 비어 있지 않은 호스트, 티스토리 메인 제외).
    """
    if not url:
        return None
    candidate = url.strip()
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or host in _MAIN_SITE_HOSTS:
        return None
    if _is_manage_path(parsed.path) or not _PERMALINK_PATH.match(parsed.path):
        return None
    return candidate


def _is_public_label(text: str | None) -> bool:
    """버튼 문구가 '공개'를 말하는가. '비공개'를 빼고도 '공개'가 남으면 공개 문구로 본다(공개 발행, 공개 저장 …)."""
    squashed = _WHITESPACE.sub("", text or "")
    return "공개" in squashed.replace("비공개", "")


def _is_draft_save_label(text: str | None) -> bool:
    """'임시저장'(초안 저장) 버튼인가. has-text('저장') 후보에 함께 걸리지만 발행 버튼이 아니므로 최종 버튼으로 누르지 않는다."""
    return "임시" in _WHITESPACE.sub("", text or "")


def _is_private_final_label(text: str | None) -> bool:
    """최종 버튼 문구가 비공개 발행을 말하는가(양의 증거). '비공개'가 있고, '공개'(비공개 제외)나 '임시저장'이 같이 있지 않아야 한다.

    '공개가 아니다'는 것만으로는 부족하다. '발행하기'처럼 중립적인 문구는 공개 글을 올리는 버튼일 수 있어 인정하지 않는다.
    """
    squashed = _WHITESPACE.sub("", text or "")
    return REQUIRED_FINAL_LABEL in squashed and not _is_public_label(text) and not _is_draft_save_label(text)


def _is_layer_opener_label(text: str | None) -> bool:
    """발행 레이어를 여는 버튼인가: 문구가 정확히 '완료'일 때만. '발행 완료'·'공개 발행' 같은 문구는 글을 올리는 버튼일 수 있다."""
    return _WHITESPACE.sub("", text or "") == LAYER_OPENER_LABEL


def _anonymous_user_agent(browser: Any) -> str:
    """익명 방문용 UA. 헤드리스 브라우저의 기본 UA는 'HeadlessChrome'이라 봇 차단에 걸리기 쉬워 일반 Chrome 표기로 바꾼다."""
    version = getattr(browser, "version", None)
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        version = ANONYMOUS_FALLBACK_CHROME_VERSION
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"


def _site_root(url: str) -> str:
    """글 주소가 속한 블로그의 홈 주소(스킴 + 호스트[:포트] + /)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _same_site(first_url: str, second_url: str) -> bool:
    """두 주소의 호스트가 같은가(www. 접두사 차이와 대소문자는 무시)."""

    def host(url: str) -> str:
        name = (urlparse(url).hostname or "").lower().rstrip(".")
        return name[4:] if name.startswith("www.") else name

    return bool(host(first_url)) and host(first_url) == host(second_url)


def _class_says_selected(class_attr: str | None) -> bool:
    return any(_SELECTED_CLASS_TOKEN.match(token) for token in (class_attr or "").lower().split())


def _redirected_away(requested_url: str, final_url: str) -> bool:
    """익명 방문이 글 페이지가 아닌 곳(로그인·홈·관리 화면 등)으로 옮겨졌는가.

    경로가 같은 이동(http→https, 커스텀 도메인)이나 퍼머링크 사이의 이동(/123 → /entry/슬러그 정규화)은 해당하지 않는다.
    about:blank·chrome-error:// 처럼 실제 웹 페이지가 아닌 최종 주소도 '이동했다'는 증거로 쓰지 않는다.
    """
    final = urlparse(final_url)
    if final.scheme not in ("http", "https") or not final.hostname:
        return False
    requested_path = urlparse(requested_url).path.rstrip("/")
    if final.path.rstrip("/") == requested_path:
        return False
    return _PERMALINK_PATH.match(final.path) is None


def _anonymous_private_evidence(
    *,
    status: int | None,
    requested_url: str,
    final_url: str,
    content: str,
    draft: PublicationDraft,
    marker_present: bool,
) -> str:
    """익명 방문자가 글을 읽을 수 없다는 '양의 증거'를 돌려준다. 없거나 글이 보이면 PUBLISH_UNVERIFIED.

    받아들이는 증거(실제 Tistory로 검증되지 않은 휴리스틱):
      1) HTTP 401/403/404  2) 글 페이지가 아닌 경로로의 리다이렉트(로그인 등)  3) private_marker 문구
    '제목이 안 보인다'만으로는 증거가 아니다(캡차·빈 화면·이스케이프 차이에도 안 보인다).
    관리 화면(/manage…) 경로가 아닌데 제목이나 본문 앞부분이 보이면 공개 글로 보고 실패시킨다.
    """
    final_path = urlparse(final_url).path
    if not _is_manage_path(final_path) and (
        _contains_text(content, draft.title) or _contains_words(content, _leading_words(draft.body_html))
    ):
        raise PublisherFailure(
            "PUBLISH_UNVERIFIED",
            f"비로그인 상태에서 글 내용이 보입니다. 공개 글일 수 있습니다 (post URL: {requested_url})",
            post_url=requested_url,
        )
    if status in _NOT_READABLE_STATUSES:
        return f"HTTP {status}"
    if _redirected_away(requested_url, final_url):
        return f"redirected to {final_url}"
    if marker_present:
        return "private marker"
    raise PublisherFailure(
        "PUBLISH_UNVERIFIED",
        "비로그인 상태에서 글이 읽히지 않는다는 증거(HTTP 401/403/404, 글 페이지 밖으로의 이동, 비공개 문구)를 찾지 못했습니다 "
        f"(HTTP {status}, 최종 주소: {final_url}, post URL: {requested_url})",
        post_url=requested_url,
    )


@dataclass(frozen=True)
class LocatorSpec:
    strategy: str
    value: str = ""
    role: str = ""
    name: str = ""


@dataclass(frozen=True)
class PrivateProof:
    """비공개 선택이 증명된 컨트롤과 그 근거. 최종 버튼을 누르기 직전에 같은 컨트롤로 다시 확인한다."""

    evidence: str
    target: Any


class TistoryPublisher:
    """Playwright adapter that fails closed when configured UI evidence is absent."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.selectors = _load_selectors(settings.tistory_selectors_path, settings.tistory_expected_blog_name)
        # 최종 발행 버튼을 누르려고 시도했는가. 이후의 모든 예외는 '글이 이미 있을 수 있음'(PUBLISH_UNVERIFIED)으로 다룬다.
        self._final_click_attempted = False
        # '공개'를 말해서 취소한 대화상자 문구. 하나라도 있으면 그 시점부터 발행을 이어가지 않는다.
        self._public_dialogs: list[str] = []

    def open_login(self) -> None:
        """티스토리 메인/로그인 페이지를 열어 로그인 세션 쿠키를 수동 갱신."""
        with sync_playwright() as playwright:
            self.settings.tistory_profile_path.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                str(self.settings.tistory_profile_path), headless=False
            )
            try:
                page = context.new_page()
                page.goto("https://www.tistory.com", wait_until="domcontentloaded")
                input("Complete login in the browser, then press Enter to close it. ")
            finally:
                context.close()

    def publish(self, draft: PublicationDraft) -> PublishedPost:
        """티스토리 비공개 포스팅 자동 작성 및 최종 발행. 비공개가 증명되지 않으면 최종 버튼을 누르지 않는다."""
        if draft.visibility != "PRIVATE":
            raise PublisherFailure("PREFLIGHT_FAILED", "only PRIVATE visibility may be published")
        self._final_click_attempted = False
        self._public_dialogs = []
        post_url: str | None = None
        page: Page | None = None
        with sync_playwright() as playwright:
            try:
                context = self._open_persistent_context(playwright)
            except Exception as error:
                # 브라우저가 뜨지 못했으니 아무것도 눌리지 않았고 글도 없다(프로필이 다른 창에서 열려 있는 경우가 흔하다).
                raise PublisherFailure(
                    "BROWSER_LAUNCH_FAILED",
                    "Tistory 브라우저를 시작하지 못했습니다(같은 프로필을 쓰는 다른 브라우저 창·워커가 열려 있지 않은지 확인하세요): "
                    f"{type(error).__name__}: {error}",
                    retryable=True,
                ) from error
            try:
                page = context.new_page()
                # 팝업 광고 및 다른 블로그 신규 탭 생성 방지 이중 가드 탑재
                self._setup_anti_popup_guard(context, page)

                page.on("dialog", self._on_dialog)

                # 1. 메인 페이지 진입 및 로그인 세션 확인
                page.goto("https://www.tistory.com", wait_until="domcontentloaded")
                self._ensure_expected_blog(page)

                # 2. 개별 블로그 포스팅 에디터 URL로 이동 및 에디터 작성 (비공개 증명 후에만 최종 버튼을 누른다)
                self._navigate_to_editor(page)
                self._fill_editor(page, draft)

                # 3. 발행 후 글 주소 확정 → 소유자 세션 내용 확인. 주소를 알게 된 뒤의 실패에는 주소를 실어 보낸다.
                self._wait_for_permalink(page)
                post_url = self._resolve_post_url(page, draft)
                self._verify_owner_content(page, post_url, draft)
                return PublishedPost(post_url)
            except PublisherFailure as failure:
                converted = self._failure_after_attempt(failure, post_url or self._salvage_post_url(page))
                if converted is failure:
                    raise
                raise converted from failure
            except Exception as error:
                raise self._failure_after_attempt(error, post_url or self._salvage_post_url(page)) from error
            finally:
                self._close_quietly(context)

    def _on_dialog(self, dialog: Any) -> None:
        """에디터가 띄운 confirm/alert/beforeunload 처리: 모두 로그로 남기고, '공개'를 말하는 것만 취소(발행 중단)한다.

        '이 글은 공개로 발행됩니다. 계속하시겠습니까?' 같은 확인창을 무조건 승인하면 비공개를 골랐어도 공개 글이 올라갈 수 있다.
        나머지(임시저장 불러오기, 페이지 이동 확인 등)는 예전처럼 승인한다.
        """
        message = str(getattr(dialog, "message", "") or "")
        kind = str(getattr(dialog, "type", "") or "")
        says_public = _is_public_label(message)
        log_event(
            logger, "tistory_dialog", dialog_type=kind, message=message[:300], handled="dismissed" if says_public else "accepted"
        )
        try:
            if says_public:
                self._public_dialogs.append(message)
                dialog.dismiss()
            else:
                dialog.accept()
        except PlaywrightError as error:
            logger.warning("dialog handling failed: %s", error)

    def _raise_if_public_dialog(self) -> None:
        """'공개'를 말하는 대화상자를 취소한 적이 있으면 발행 흐름을 멈춘다(최종 클릭 이후라면 PUBLISH_UNVERIFIED로 바뀐다)."""
        if self._public_dialogs:
            raise PublisherFailure(
                "UI_BROKEN",
                "공개 발행을 알리는 대화상자가 떠서 취소하고 발행을 멈췄습니다: " + "; ".join(message[:120] for message in self._public_dialogs),
            )

    def _wait_for_permalink(self, page: Page) -> None:
        """발행 뒤 브라우저가 글 퍼머링크로 이동할 때까지 기다린다. 이동하지 않으면(관리 목록 등) 그대로 두고 다음 단계가 목록에서 찾는다."""
        try:
            page.wait_for_url(lambda url: _permalink_or_none(url) is not None, timeout=POST_URL_WAIT_MS)
        except PlaywrightError:
            pass

    def _salvage_post_url(self, page: Page | None) -> str | None:
        """최종 클릭 이후 실패했을 때, 현재 화면 주소가 글 퍼머링크라면 그것을 건져 둔다(실패에 실어 워커가 보존한다)."""
        if page is None or not self._final_click_attempted:
            return None
        try:
            return _permalink_or_none(page.url)
        except Exception:
            return None

    def _failure_after_attempt(self, error: Exception, post_url: str | None) -> PublisherFailure:
        """최종 클릭 이후의 모든 실패는 PUBLISH_UNVERIFIED(글이 이미 있을 수 있음), 이전 실패는 그대로/UI_BROKEN."""
        suffix = f" (post URL: {post_url})" if post_url else ""
        if isinstance(error, PublisherFailure):
            if not self._final_click_attempted or error.code == "PUBLISH_UNVERIFIED":
                if post_url and not error.post_url:
                    error.post_url = post_url
                return error
            return PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"최종 발행 버튼을 누른 뒤 [{error.code}] {error}. 글이 이미 만들어졌을 수 있습니다{suffix}",
                post_url=post_url,
            )
        if self._final_click_attempted:
            return PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"최종 발행 버튼을 누른 뒤 오류가 발생했습니다. 글이 이미 만들어졌을 수 있습니다: {error}{suffix}",
                post_url=post_url,
            )
        return PublisherFailure("UI_BROKEN", f"required Tistory editor control was unavailable: {error}")

    def verify_private(self, post: PublishedPost, draft: PublicationDraft) -> str:
        """비로그인 익명 세션에서 비공개 포스팅 접근 차단 여부 검증. 비공개로 판단한 근거 문자열을 돌려준다."""
        if _permalink_or_none(post.url) is None:
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"글 퍼머링크 형식이 아닌 주소라 비공개를 확인할 수 없습니다 (post URL: {post.url})",
                post_url=post.url,
            )
        with sync_playwright() as playwright:
            browser: Any = None
            try:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=_anonymous_user_agent(browser))
                # 대조군: 같은 익명 브라우저로 블로그 홈이 열려야 한다. 안 열리면 봇 차단·점검 등이라 글에 대한 401/403/404도 믿을 수 없다.
                self._check_anonymous_control(page, post.url)
                first = self._anonymous_visit(page, post, draft)
                # CDN 캐시·늦은 렌더링으로 첫 방문만 막힌 공개 글을 걸러내려고 간격을 두고 한 번 더 같은 판단을 한다.
                page.wait_for_timeout(ANONYMOUS_RECHECK_DELAY_MS)
                second = self._anonymous_visit(page, post, draft)
                return first if first == second else f"{first}; {second}"
            except PublisherFailure:
                raise
            except Exception as error:
                raise PublisherFailure(
                    "PUBLISH_UNVERIFIED",
                    f"private post verification failed: {error} (post URL: {post.url})",
                    post_url=post.url,
                ) from error
            finally:
                if browser is not None:
                    self._close_quietly(browser)

    @staticmethod
    def _settle_page(page: Page) -> None:
        """페이지가 자리 잡기를 기다린다(클라이언트 렌더링 글이 아직 안 그려진 채로 판단하지 않도록). 시간 초과는 무시한다."""
        try:
            page.wait_for_load_state("networkidle", timeout=ANONYMOUS_NETWORK_IDLE_MS)
        except PlaywrightError:
            pass  # 광고·추적 스크립트가 끝나지 않아도 판단은 계속한다
        page.wait_for_timeout(ANONYMOUS_SETTLE_MS)

    def _check_anonymous_control(self, page: Page, post_url: str) -> None:
        """익명 방문이 이 블로그의 공개 페이지(홈)를 HTTP 200으로 읽을 수 있는지 확인한다. 아니면 PUBLISH_UNVERIFIED."""
        root = _site_root(post_url)
        response = page.goto(root, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        # 홈이 다른 사이트나 같은 사이트의 다른 경로(/captcha, /auth/login 등)로 옮겨졌다면 모든 주소를 막는 차단·로그인 벽이다.
        at_root = urlparse(page.url).path in ("", "/")
        if status != 200 or not _same_site(root, page.url) or not at_root:
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"익명 방문의 대조군인 블로그 홈({root})이 정상적으로 열리지 않아(HTTP {status}, 최종 주소: {page.url}) "
                f"비공개 여부를 판단할 수 없습니다. 봇 차단이나 점검일 수 있습니다 (post URL: {post_url})",
                post_url=post_url,
            )

    def _anonymous_visit(self, page: Page, post: PublishedPost, draft: PublicationDraft) -> str:
        """글 주소를 익명으로 한 번 열어 '읽을 수 없다'는 증거를 돌려준다(없으면 PUBLISH_UNVERIFIED)."""
        response = page.goto(post.url, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        self._settle_page(page)
        return _anonymous_private_evidence(
            status=status if isinstance(status, int) else None,
            requested_url=post.url,
            final_url=page.url,
            content=page.content(),
            draft=draft,
            marker_present=self._locator(page, "private_marker").count() > 0,
        )

    @staticmethod
    def _close_quietly(resource: Any) -> None:
        """브라우저 정리 실패가 이미 얻은 결과(글 주소·예외)를 덮어쓰지 않게 한다."""
        try:
            resource.close()
        except Exception as error:
            logger.warning("browser cleanup failed: %s", error)

    def _locator(self, page: Page, name: str) -> Locator:
        """설정된 선택자 전략으로 Locator 객체 정밀 생성 및 안전 fallback."""
        if name not in self.selectors:
            if name == "identity":
                return page.locator("a[href*='manage'], .txt_id, .link_profile, .thumb_profile, img[alt*='프로필']")
            if name == "title":
                return page.locator("#post-title-inp, textarea[placeholder*='제목'], input[placeholder*='제목']")
            if name == "private_marker":
                return page.locator(".txt_private, :has-text('권한이 없습니다'), :has-text('비공개')")
            if name == "recent_post_link":
                return page.locator("a[href*='/manage/newpost'], a.link_post, .list_post a, a[href*='entry']")
            return page.locator(f":has-text('{name}')")

        spec = self.selectors[name]
        if spec.strategy == "role":
            return page.get_by_role(spec.role, name=spec.name)
        if spec.strategy == "text":
            return page.get_by_text(spec.value, exact=True)
        if spec.strategy == "placeholder":
            return page.get_by_placeholder(spec.value)
        if spec.strategy == "css":
            return page.locator(spec.value)
        return page.locator(spec.value if hasattr(spec, 'value') else "body")

    def _setup_anti_popup_guard(self, context: BrowserContext, main_page: Page) -> None:
        """광고 팝업 스크립트 무력화 및 신규 탭/팝업 발생 시 즉시 자동 닫기(close) 가드."""
        try:
            main_page.add_init_script("window.open = function() { return null; };")
        except Exception:
            pass

        def handle_new_page(new_page: Page) -> None:
            try:
                if new_page != main_page:
                    new_page.close()
            except Exception:
                pass

        try:
            context.on("page", handle_new_page)
        except Exception:
            pass

    def _open_persistent_context(self, playwright: Any) -> BrowserContext:
        """저장된 퍼시스턴트 브라우저 프로필 콘텍스트 오픈."""
        self.settings.tistory_profile_path.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(str(self.settings.tistory_profile_path), headless=False)

    def _ensure_expected_blog(self, page: Page) -> None:
        """블로그 식별자 확인, 로그인 버튼 클릭 및 카카오 첫 번째 계정 자동 선택 처리."""
        if self._locator(page, "identity").count() > 0:
            return

        if self._handle_kakao_account_selection(page):
            if self._locator(page, "identity").count() > 0:
                return

        clicked = self._try_click_login_button(page)
        if clicked:
            page.wait_for_timeout(2_000)
            self._handle_kakao_account_selection(page)

        if self._locator(page, "identity").count() == 0:
            try:
                page.goto("https://www.tistory.com/auth/login", wait_until="domcontentloaded")
                page.wait_for_timeout(1_000)
                self._try_click_login_button(page)
                page.wait_for_timeout(2_000)
                self._handle_kakao_account_selection(page)
                if page.url != "https://www.tistory.com":
                    page.goto("https://www.tistory.com", wait_until="domcontentloaded")
                    page.wait_for_timeout(1_000)
            except Exception:
                pass

        if self._locator(page, "identity").count() == 0:
            raise PublisherFailure("AUTH_REQUIRED", "expected blog identity was not visible")

    def _navigate_to_editor(self, page: Page) -> None:
        """설정된 TISTORY_WRITE_URL(개별 블로그 manage/newpost) 포스팅 에디터 페이지로 이동 및 요소 안전 대기."""
        if self._is_editor_present(page):
            return

        try:
            page.goto(self.settings.tistory_write_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)
            if self._is_editor_present(page):
                return
        except Exception:
            pass

        for edit_btn_selector in [
            "a:has-text('글쓰기')",
            "button:has-text('글쓰기')",
            ".link_edit",
            "a[href*='manage/newpost']",
            "a[href*='manage/post']",
            "a[href*='manage/entry/post']",
        ]:
            loc = page.locator(edit_btn_selector)
            if loc.count() > 0 and loc.first.is_visible():
                try:
                    loc.first.click()
                    page.wait_for_timeout(2_000)
                    if self._is_editor_present(page):
                        return
                except Exception:
                    continue

    def _is_editor_present(self, page: Page) -> bool:
        """현재 페이지에 포스팅 에디터 요소가 존재하는지 명시적 안전 대기 후 확인."""
        try:
            page.wait_for_selector("#editor-mode-layer-btn-open, #post-title-inp, textarea[placeholder*='제목']", timeout=5_000)
            return True
        except Exception:
            return page.locator("#editor-mode-layer-btn-open").count() > 0 or self._locator(page, "title").count() > 0 or page.locator("textarea[placeholder*='제목']").count() > 0

    def _try_click_login_button(self, page: Page) -> bool:
        """현재 페이지 화면의 카카오 로그인 관련 버튼을 탐색하여 원클릭 실행."""
        login_selectors = [
            "a:has-text('카카오계정으로 로그인')",
            "button:has-text('카카오계정으로 로그인')",
            ".btn_login.link_kakao",
            ".btn_login",
            "a.link_login",
            "a:has-text('카카오 로그인')",
            "a:has-text('시작하기')",
            "a:has-text('로그인')",
        ]
        for sel in login_selectors:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                try:
                    loc.first.click()
                    return True
                except Exception:
                    continue
        return False

    def _handle_kakao_account_selection(self, page: Page) -> bool:
        """'로그인할 카카오 계정 선택' 화면이 뜬 경우 첫 번째 계정 자동 클릭."""
        account_selectors = [
            "ul.list_account li:first-child button",
            "ul.list_account li:first-child a",
            "ul.list_account li:first-child",
            ".list_account .btn_account",
            ".list_account .item_account",
            ".list_account .link_account",
            ".item_account",
            ".link_account",
            "button:has-text('@')",
            "a:has-text('@')",
        ]
        for sel in account_selectors:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                try:
                    loc.first.click()
                    page.wait_for_timeout(3_000)
                    return True
                except Exception:
                    continue
        return False

    def _select_category(self, page: Page, category_name: str) -> None:
        """에디터 상단 카테고리 선택 후 Escape 키로 잔여 드롭다운 팝업 레이어를 완전 닫음."""
        if not category_name:
            return
        try:
            # 1. 카테고리 드롭다운 클릭
            cat_btn_selectors = [
                "#category-btn",
                ".btn-category",
                "button:has-text('카테고리')",
                "#category-select",
                ".btn_category",
                "span:has-text('카테고리')",
            ]
            for sel in cat_btn_selectors:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(force=True)
                    page.wait_for_timeout(600)
                    break

            # 2. 카테고리 목록에서 지정된 카테고리 텍스트 클릭
            item_selectors = [
                f"ul.list_category li:has-text('{category_name}')",
                f"div.option-list li:has-text('{category_name}')",
                f"button:has-text('{category_name}')",
                f"a:has-text('{category_name}')",
                f"span:has-text('{category_name}')",
                f"li:has-text('{category_name}')",
            ]
            for isel in item_selectors:
                iloc = page.locator(isel)
                if iloc.count() > 0 and iloc.first.is_visible():
                    iloc.first.click(force=True)
                    page.wait_for_timeout(600)
                    break

            # 3. 잔여 팝업 레이어 닫기
            page.keyboard.press("Escape")
            page.wait_for_timeout(1_000)
        except Exception as error:
            # 카테고리는 best-effort: 실패해도 발행을 막지 않는다(로그만 남김).
            logger.warning("category selection failed (continuing): %s", error)

    def _switch_to_html_mode(self, page: Page) -> None:
        """#editor-mode-layer-btn-open 과 #editor-mode-layer-btn-html exact ID 단독 지정 클릭."""
        try:
            # 1. 우측 상단 모드 드롭다운 exact ID 단독 클릭
            open_btn = page.locator("#editor-mode-layer-btn-open, button#editor-mode-layer-btn-open")
            if open_btn.count() > 0:
                open_btn.first.click(force=True)
                page.wait_for_timeout(800)

            # 2. HTML 모드 exact ID 단독 클릭
            html_btn = page.locator("#editor-mode-layer-btn-html, [data-mode='html']")
            if html_btn.count() > 0:
                html_btn.first.click(force=True)
                page.wait_for_timeout(1_000)
            else:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(150)
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(150)
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(150)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1_000)

            # 3. 모달 확인 승인
            confirm_loc = page.locator("button:has-text('확인'), .btn_confirm, button.btn-confirm")
            if confirm_loc.count() > 0 and confirm_loc.first.is_visible():
                confirm_loc.first.click(force=True)
                page.wait_for_timeout(1_000)
        except Exception as error:
            # 모드 전환 실패는 best-effort: 본문이 실제로 들어갔는지는 _verify_editor_content가 되읽어 판단한다.
            logger.warning("HTML mode switch failed (continuing): %s", error)

    def _upload_thumbnail(self, page: Page, thumbnail_path: Path | None) -> None:
        """썸네일 경로를 윈도우 절대 경로(Absolute Path)로 변환하여 100% 파일 정밀 업로드."""
        if not thumbnail_path:
            return
        abs_path = Path(thumbnail_path).resolve()
        if not abs_path.is_file():
            return
        try:
            file_inp = page.locator("input[type='file']")
            if file_inp.count() > 0:
                file_inp.first.set_input_files(str(abs_path))
                page.wait_for_timeout(2_500)
        except Exception as error:
            # 썸네일은 best-effort: 실패해도 발행을 막지 않는다(로그만 남김).
            logger.warning("thumbnail upload failed (continuing): %s", error)

    def _fill_editor(self, page: Page, draft: PublicationDraft) -> None:
        """CodeMirror.setValue() 및 cm.save() 단 1회 전품목 단독 주입 (중복 키보드 입력 전면 삭제)."""
        # 에디터 UI 구성 요소 명시적 안전 대기
        try:
            page.wait_for_selector("#editor-mode-layer-btn-open, #post-title-inp, textarea[placeholder*='제목']", timeout=10_000)
        except Exception:
            pass

        # 1. 카테고리 지정 및 팝업 닫기
        self._select_category(page, draft.category)

        # 2. exact ID로 HTML 모드로 100% 직행
        self._switch_to_html_mode(page)

        # 3. 제목 정밀 입력 (#post-title-inp 전용 지정)
        title_locator = page.locator("#post-title-inp, textarea[placeholder*='제목'], input[placeholder*='제목']")
        title_field = title_locator.first if title_locator.count() > 0 else self._locator(page, "title").first
        title_field.fill(draft.title)

        page.wait_for_timeout(500)

        # 4. 원본 정제 HTML 본문 사용
        clean_html = draft.body_html

        # 5. absolute path 썸네일 파일 정식 업로드
        self._upload_thumbnail(page, draft.thumbnail_path)

        # 6. CodeMirror.setValue() 및 cm.save() 단 1회 단독 주입 (중복 키보드 insert_text 전면 제거)
        try:
            page.evaluate(_INJECT_BODY_SCRIPT, clean_html)
            page.wait_for_timeout(800)
        except Exception as error:
            raise PublisherFailure("UI_BROKEN", f"본문 주입 스크립트가 실패해 발행하지 않았습니다: {error}") from error

        # 6-1. 제목·본문이 실제로 에디터에 들어갔는지 되읽어 확인한다. 빈 글/엉뚱한 글은 발행 단계로 넘기지 않는다.
        self._verify_editor_content(page, title_field, draft)

        # 7. 태그 입력
        try:
            tag_inp = page.locator("#tag-inp, input[placeholder*='태그'], input[placeholder*='#태그']")
            if tag_inp.count() > 0 and tag_inp.first.is_visible():
                tag_inp.first.fill(",".join(draft.tags))
                tag_inp.first.press("Enter")
        except Exception as error:
            # 태그는 best-effort: 실패해도 발행을 막지 않는다(로그만 남김).
            logger.warning("tag input failed (continuing): %s", error)

        page.wait_for_timeout(1_000)

        # 8. 하단 우측 '완료' 버튼으로 발행 레이어 팝업을 연다 (문구가 정확히 '완료'인 버튼만 누른다)
        self._open_publish_layer(page)

        # 9. 발행 레이어 팝업: 비공개를 선택·증명한 뒤에만 최종 버튼을 누른다
        self._publish_as_private(page, draft)

    def _open_publish_layer(self, page: Page) -> None:
        """'완료' 버튼으로 발행 레이어를 연다. 레이어(공개 범위 옵션)가 나타나는 것을 확인하지 못하면 그 클릭이 글을 올렸을 수 있다고 본다.

        최종 발행 버튼이 될 수 있는 #publish-btn 이나 '발행' 문구 버튼은 열기 버튼으로 누르지 않는다.
        누를 '완료' 버튼이 없으면 아무것도 누르지 않은 채 돌아가고, 레이어가 안 열려 있으면 다음 단계가 UI_BROKEN으로 멈춘다.
        레이어는 열렸는데 비공개 옵션만 없는 경우(UI 개편)도 다음 단계가 UI_BROKEN으로 멈춘다(레이어가 보이니 글은 올라가지 않았다).
        """
        opener = self._find_layer_opener(page)
        if opener is None:
            logger.warning("no '완료' button to open the publish layer was found; nothing was clicked")
            return
        try:
            opener.click(force=True, timeout=CLICK_TIMEOUT_MS)
        except PlaywrightError as error:
            # 클릭이 나갔는지 알 수 없다(내비게이션 대기 타임아웃 포함): 글이 올라갔을 수 있다고 본다.
            self._final_click_attempted = True
            raise PublisherFailure("UI_BROKEN", f"'완료' 버튼 클릭 결과를 확인하지 못했습니다: {error}") from error
        self._raise_if_public_dialog()
        if not self._wait_for_publish_layer(page):
            self._final_click_attempted = True
            raise PublisherFailure(
                "UI_BROKEN",
                "'완료'를 눌렀지만 발행 레이어(공개 범위 옵션)가 나타나지 않았습니다. 그 클릭이 글을 올렸을 수 있습니다",
            )

    @staticmethod
    def _find_layer_opener(page: Page) -> Locator | None:
        """보이는 '완료' 버튼(문구가 정확히 '완료')의 첫 번째."""
        for selector in LAYER_OPENER_SELECTORS:
            for button in page.locator(selector).all():
                try:
                    if button.is_visible() and _is_layer_opener_label(button.inner_text()):
                        return button
                except PlaywrightError:
                    continue
        return None

    def _wait_for_publish_layer(self, page: Page) -> bool:
        """발행 레이어가 열렸다는 표시(보이는 공개 범위 옵션: 비공개/보호/공개)가 나타날 때까지(최대 LAYER_OPEN_WAIT_MS) 기다린다."""
        waited = 0
        while True:
            for selector in (*PRIVATE_OPTION_SELECTORS, *NON_PRIVATE_OPTION_SELECTORS):
                try:
                    if self._first_visible(page, selector) is not None:
                        return True
                except PlaywrightError:
                    continue  # 화면이 바뀌는 중이면 이번 확인은 못 본 것으로 친다
            if waited >= LAYER_OPEN_WAIT_MS:
                return False
            page.wait_for_timeout(LAYER_POLL_MS)
            waited += LAYER_POLL_MS

    def _publish_as_private(self, page: Page, draft: PublicationDraft) -> None:
        """발행 레이어에서 '비공개'를 고르고 그 선택을 증명한 뒤에만 최종 발행 버튼을 누른다.

        증명하지 못하면(옵션 없음·숨김·클릭 무효 등) 최종 버튼을 한 번도 누르지 않고 UI_BROKEN으로 멈춘다.
        여기서는 예외를 삼키지 않고 force 클릭도 쓰지 않는다. 누를 수 있는 최종 버튼이 없어도 조용히 성공하지 않는다.
        """
        if draft.visibility != "PRIVATE":
            raise PublisherFailure("PREFLIGHT_FAILED", "only PRIVATE visibility may be published")
        self._upload_thumbnail(page, draft.thumbnail_path)
        proof = self._select_private_visibility(page)
        log_event(logger, "private_visibility_proven", evidence=proof.evidence)
        self._raise_if_public_dialog()
        self._click_final_publish_button(page, proof)

    def _select_private_visibility(self, page: Page) -> PrivateProof:
        """비공개 옵션을 눌러 보고 '선택됨'이 증명되는 첫 후보를 돌려준다. 증명 불가면 UI_BROKEN."""
        notes: list[str] = []
        for selector in PRIVATE_OPTION_SELECTORS:
            target = self._first_visible(page, selector)
            if target is None:
                notes.append(f"{selector}: 보이는 요소 없음")
                continue
            if selector.startswith("input") and (target.get_attribute("type") or "").lower() != "radio":
                notes.append(f"{selector}: 라디오가 아님")
                continue
            try:
                target.click(timeout=CLICK_TIMEOUT_MS)
                page.wait_for_timeout(500)
                evidence = self._selection_evidence(target)
            except PlaywrightError as error:
                notes.append(f"{selector}: 클릭/확인 실패({type(error).__name__})")
                continue
            if evidence is None:
                notes.append(f"{selector}: 클릭했지만 선택 상태를 확인하지 못함")
                continue
            # 비공개가 선택됐다고 해도 보호/공개 옵션이 선택돼 있다면 증명이 깨진 것이다.
            conflicting = self._checked_non_private_option(page)
            if conflicting is not None:
                raise PublisherFailure(
                    "UI_BROKEN",
                    f"비공개 선택 후에도 다른 공개 범위({conflicting})가 선택돼 있어 발행하지 않았습니다",
                )
            return PrivateProof(evidence=f"{selector} ({evidence})", target=target)
        raise PublisherFailure(
            "UI_BROKEN",
            "비공개 선택을 확인하지 못해 발행하지 않았습니다 (최종 발행 버튼은 누르지 않음): " + "; ".join(notes),
        )

    @staticmethod
    def _first_visible(page: Page, selector: str) -> Locator | None:
        """선택자에 일치하는 요소 중 보이는 첫 번째(숨겨진 input이 앞에 있어도 건너뛴다)."""
        locator = page.locator(selector)
        if locator.count() == 0:
            return None
        for candidate in locator.all():
            if candidate.is_visible():
                return candidate
        return None

    @staticmethod
    def _selection_evidence(target: Locator) -> str | None:
        """방금 누른 비공개 컨트롤이 선택된 상태인지 되읽는다. 근거 문자열, 증명 불가면 None.

        라디오(또는 라벨 → 연결된 라디오)는 is_checked()로 판정하고, False면 그대로 '선택 안 됨'이다.
        input이 아닌 커스텀 컨트롤(is_checked()가 지원되지 않을 때)만 aria-checked/aria-pressed가 "true"이거나
        클래스 토큰이 checked/active/selected/on(예: is-checked)인 경우를 선택됨으로 인정한다.
        """
        try:
            return "is_checked" if target.is_checked() else None
        except PlaywrightError:
            pass  # 체크박스/라디오가 아닌 커스텀 컨트롤: 아래 속성으로 판정
        if (target.get_attribute("aria-checked") or "").lower() == "true":
            return "aria-checked"
        if (target.get_attribute("aria-pressed") or "").lower() == "true":
            return "aria-pressed"
        if _class_says_selected(target.get_attribute("class")):
            return "class"
        return None

    @staticmethod
    def _checked_non_private_option(page: Page) -> str | None:
        """보호/공개 라디오 중 선택된 것이 있으면 그 선택자를 돌려준다."""
        for selector in NON_PRIVATE_OPTION_SELECTORS:
            for radio in page.locator(selector).all():
                if radio.is_checked():
                    return selector
        return None

    def _click_final_publish_button(self, page: Page, proof: PrivateProof) -> None:
        """비공개 발행을 말하는 최종 버튼(문구에 '비공개'가 있고 공개·임시저장 문구가 아닌 첫 보이는 후보)을 누른다.

        누르기 직전에 비공개 선택과 버튼 문구를 한 번 더 읽어 확인한다(레이어가 늦게 기본값으로 되돌리는 경우).
        누를 수 있는 버튼이 없으면 UI_BROKEN(조용히 성공하지 않음).
        """
        skipped: list[str] = []
        for selector in FINAL_BUTTON_SELECTORS:
            for button in page.locator(selector).all():
                if not button.is_visible():
                    continue
                try:
                    label = button.inner_text()
                except PlaywrightError as error:
                    skipped.append(f"{selector}: 문구를 읽지 못함({type(error).__name__})")
                    continue
                if _is_public_label(label):
                    skipped.append(f"{selector}: 공개 문구({label.strip()!r})")
                    continue
                if _is_draft_save_label(label):
                    skipped.append(f"{selector}: 임시저장 버튼({label.strip()!r})")
                    continue
                if not _is_private_final_label(label):
                    skipped.append(f"{selector}: 문구에 '{REQUIRED_FINAL_LABEL}'가 없음({label.strip()!r})")
                    continue
                self._recheck_before_final_click(page, proof, button)
                self._arm_click_guard(button, proof)
                # 이 시점부터는 클릭이 실제로 나갔는지 알 수 없으므로(타임아웃·내비게이션 포함) 글이 있을 수 있다고 본다.
                self._final_click_attempted = True
                button.click(timeout=CLICK_TIMEOUT_MS)
                page.wait_for_timeout(5_000)
                self._raise_if_click_was_blocked(page)
                self._raise_if_public_dialog()
                return
        raise PublisherFailure(
            "UI_BROKEN",
            "누를 수 있는 안전한 최종 발행 버튼(문구에 '비공개'가 있는 버튼)을 찾지 못해 발행하지 않았습니다"
            + (f" (건너뜀: {'; '.join(skipped)})" if skipped else ""),
        )

    @staticmethod
    def _arm_click_guard(button: Locator, proof: PrivateProof) -> None:
        """최종 버튼의 클릭 이벤트마다 비공개 상태를 페이지 안에서 다시 확인하는 가드를 설치한다. 설치하지 못하면 누르지 않는다."""
        try:
            armed = button.evaluate(
                _CLICK_GUARD_SCRIPT, proof.target.element_handle(timeout=CLICK_TIMEOUT_MS), timeout=CLICK_TIMEOUT_MS
            )
        except PlaywrightError as error:
            raise PublisherFailure(
                "UI_BROKEN", f"최종 클릭 가드를 설치하지 못해 발행하지 않았습니다 (최종 발행 버튼은 누르지 않음): {error}"
            ) from error
        if armed is not True:
            raise PublisherFailure(
                "UI_BROKEN", "최종 클릭 가드를 설치하는 순간 비공개 상태가 아님을 확인해 발행하지 않았습니다 (최종 발행 버튼은 누르지 않음)"
            )

    @staticmethod
    def _raise_if_click_was_blocked(page: Page) -> None:
        """가드가 클릭을 취소했다면(클릭 순간 비공개 상태가 아니었다) 발행 흐름을 멈춘다. 클릭 뒤 화면이 이동했다면 읽을 수 없고 0으로 본다."""
        try:
            blocked = page.evaluate(_GUARD_BLOCKED_SCRIPT)
        except PlaywrightError:
            return
        if isinstance(blocked, int) and not isinstance(blocked, bool) and blocked > 0:
            raise PublisherFailure(
                "UI_BROKEN",
                f"클릭 순간 비공개 상태가 아님을 확인한 가드가 최종 클릭을 취소했습니다(취소한 이벤트 {blocked}개). 글이 올라가지 않았을 가능성이 높지만 확인이 필요합니다",
            )

    def _recheck_before_final_click(self, page: Page, proof: PrivateProof, button: Locator) -> None:
        """최종 클릭 직전 재확인: 비공개 선택이 아직 유지되는지, 다른 공개 범위가 켜지지 않았는지, 버튼 문구가 그대로인지.

        선택을 증명한 뒤 500ms 이상 지난 시점에 레이어가 기본값(공개)으로 되돌려지는 경우를 잡는다.
        (남는 틈은 이 확인과 실제 클릭 사이의 수~수십 ms뿐이다.)
        """
        try:
            # 가장 중요한 읽기(비공개 선택 유지 여부)를 마지막에 둬서 이 확인과 클릭 사이의 틈을 줄인다.
            conflicting = self._checked_non_private_option(page)
            label = button.inner_text()
            still_selected = self._selection_evidence(proof.target)
        except PlaywrightError as error:
            raise PublisherFailure(
                "UI_BROKEN", f"최종 버튼을 누르기 직전 비공개 선택을 다시 확인하지 못해 발행하지 않았습니다: {error}"
            ) from error
        if still_selected is None or conflicting is not None or not _is_private_final_label(label):
            raise PublisherFailure(
                "UI_BROKEN",
                "최종 버튼을 누르기 직전에 비공개 선택이 바뀐 것을 확인해 발행하지 않았습니다 "
                f"(선택 유지: {still_selected is not None}, 다른 공개 범위: {conflicting}, 버튼 문구: {label.strip()!r})",
            )
        self._raise_if_public_dialog()

    def _verify_editor_content(self, page: Page, title_field: Locator, draft: PublicationDraft) -> None:
        """제목·본문을 에디터에서 되읽어 확인한다. 본문이 비었거나 들어가지 않았으면 발행 전에 UI_BROKEN."""
        try:
            title_value = title_field.input_value()
        except PlaywrightError as error:
            raise PublisherFailure("UI_BROKEN", f"제목 입력값을 되읽지 못해 발행하지 않았습니다: {error}") from error
        if _normalize_text(title_value) != _normalize_text(draft.title):
            raise PublisherFailure(
                "UI_BROKEN",
                f"제목이 에디터에 그대로 입력되지 않아 발행하지 않았습니다 (입력값: {title_value[:60]!r})",
            )
        try:
            state = page.evaluate(_READ_BODY_SCRIPT)
        except Exception as error:
            raise PublisherFailure("UI_BROKEN", f"에디터 본문을 되읽지 못해 발행하지 않았습니다: {error}") from error
        if not self._body_was_injected(state, draft.body_html):
            raise PublisherFailure(
                "UI_BROKEN",
                "본문이 에디터에 들어갔는지 확인하지 못해 발행하지 않았습니다 (CodeMirror/textarea/TinyMCE 어디에서도 본문을 읽지 못함)",
            )

    @staticmethod
    def _body_was_injected(state: Any, body_html: str) -> bool:
        """읽어 온 에디터 값 중 하나라도 본문 앞부분 단어를 모두 담고 있으면 주입된 것으로 본다."""
        if not isinstance(state, dict):
            return False
        values = [state.get("codemirror"), state.get("tinymce"), *(state.get("textareas") or [])]
        words = _leading_words(body_html)
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            if not words or _contains_words(value, words):  # 단어가 없는 본문은 '비어 있지 않음'만 확인
                return True
        return False

    def _resolve_post_url(self, page: Page, draft: PublicationDraft) -> str:
        """발행 후 글 퍼머링크를 확정한다. 현재 주소가 퍼머링크가 아니면(/manage… 등) 최근 글 목록에서 찾는다."""
        current_url = page.url
        post_url = _permalink_or_none(current_url)
        if post_url is None:
            found = self._find_recent_post(page, draft.title)
            post_url = _permalink_or_none(urljoin(current_url, found)) if found else None
        if post_url is None:
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"발행 후 글 주소(퍼머링크)를 확인하지 못했습니다 (현재 주소: {current_url}). 글이 만들어졌을 수 있으니 Tistory 글 관리에서 확인하세요",
            )
        return post_url

    def _find_recent_post(self, page: Page, title: str) -> str | None:
        """최근 포스팅 목록에서 지정한 제목 포스팅의 href를 찾는다(공백 차이는 무시).

        글 주소 모양(/숫자, /entry/슬러그)인 링크만 후보로 삼는다. 같은 제목의 글이 여럿이면(재시도 등) 글 번호가 가장 큰
        (= 가장 최근) 링크를 고르고, 번호로 비교할 수 없으면(슬러그 주소가 섞인 경우) 오래된 글을 잘못 고르지 않도록 None을 돌려준다.
        """
        wanted = _normalize_text(title)
        base_url = page.url
        candidates: dict[str, str] = {}  # 절대 주소(끝의 / 제외) → 원래 href
        for link in self._locator(page, "recent_post_link").all():
            if _normalize_text(link.inner_text()) != wanted:
                continue
            href = link.get_attribute("href")
            permalink = _permalink_or_none(urljoin(base_url, href)) if href else None
            if href and permalink:
                candidates.setdefault(permalink.rstrip("/"), href)
        if len(candidates) <= 1:
            return next(iter(candidates.values()), None)
        by_number: dict[int, str] = {}
        for permalink, href in candidates.items():
            match = _NUMERIC_PERMALINK_PATH.match(urlparse(permalink).path)
            if match is None:
                return None
            by_number[int(match.group(1))] = href
        return by_number[max(by_number)]

    @staticmethod
    def _verify_owner_content(page: Page, result_url: str, draft: PublicationDraft) -> None:
        """소유자 세션에서 발행된 글을 확인한다. 제목 AND 본문 앞부분 단어 전부가 보여야 통과한다.

        받아들이는 증거(실제 Tistory로 검증되지 않은 휴리스틱): HTTP 오류가 아니고, 최종 주소가 /manage… 가 아니며,
        엔티티를 푼 페이지(원문 또는 태그 제거본)에 제목이 있고, 본문 앞 단어(최대 5개)가 모두 있다.
        주소 모양(entry/숫자)이나 제목 첫 단어만으로는 통과시키지 않는다.
        """
        response = page.goto(result_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        status = getattr(response, "status", None)
        if isinstance(status, int) and status >= 400:
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"소유자 세션에서 글 페이지가 HTTP {status}를 반환했습니다 (post URL: {result_url})",
                post_url=result_url,
            )
        if _is_manage_path(urlparse(page.url).path):
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"글 주소가 관리 화면({page.url})으로 이동해 글 내용을 확인할 수 없습니다 (post URL: {result_url})",
                post_url=result_url,
            )
        content = page.content()
        words = _leading_words(draft.body_html)
        has_title = _contains_text(content, draft.title)
        has_body = _contains_words(content, words)  # 대조할 단어가 없으면 False(증명 불가 → 실패)
        if not (has_title and has_body):
            missing = [name for name, found in (("제목", has_title), ("본문", has_body)) if not found]
            raise PublisherFailure(
                "PUBLISH_UNVERIFIED",
                f"소유자 세션에서 글 내용을 확인하지 못했습니다 (없는 항목: {', '.join(missing)}, post URL: {result_url})",
                post_url=result_url,
            )


def _load_selectors(path: Path, expected_blog_name: str) -> dict[str, LocatorSpec]:
    """YAML 설정 파일에서 티스토리 DOM 선택자 로드."""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw_selectors = payload["tistory"]
        selectors = {
            name: LocatorSpec(**{key: str(value).replace("{{TISTORY_EXPECTED_BLOG_NAME}}", expected_blog_name) for key, value in value.items()})
            for name, value in raw_selectors.items()
        }
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise PublisherFailure("UI_BROKEN", "selector configuration could not be loaded") from error
    required = {"identity", "title", "html_mode", "html_editor", "thumbnail_input", "category_menu", "tag_input", "visibility_menu", "private_option", "publish_button", "private_marker", "recent_post_link"}
    missing = required.difference(selectors)
    if missing:
        raise PublisherFailure("UI_BROKEN", f"selector configuration is missing: {', '.join(sorted(missing))}")
    return selectors
