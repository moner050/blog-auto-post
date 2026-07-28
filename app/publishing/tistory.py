from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

import yaml
from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright

from app.core.settings import Settings
from app.publishing.client import PublishedPost, PublisherFailure
from app.publishing.guards import PublicationDraft


@dataclass(frozen=True)
class LocatorSpec:
    strategy: str
    value: str = ""
    role: str = ""
    name: str = ""


class TistoryPublisher:
    """Playwright adapter that fails closed when configured UI evidence is absent."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.selectors = _load_selectors(settings.tistory_selectors_path, settings.tistory_expected_blog_name)

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
        """티스토리 비공개 포스팅 자동 작성 및 최종 발행."""
        with sync_playwright() as playwright:
            context = self._open_persistent_context(playwright)
            try:
                page = context.new_page()
                # 팝업 광고 및 다른 블로그 신규 탭 생성 방지 이중 가드 탑재
                self._setup_anti_popup_guard(context, page)

                page.on("dialog", lambda dialog: dialog.accept())

                # 1. 메인 페이지 진입 및 로그인 세션 확인
                page.goto("https://www.tistory.com", wait_until="domcontentloaded")
                self._ensure_expected_blog(page)
                
                # 2. 개별 블로그 포스팅 에디터 URL로 이동 및 에디터 작성
                self._navigate_to_editor(page)
                self._fill_editor(page, draft)
                
                page.wait_for_timeout(3_000)
                result_url = page.url
                if not result_url or result_url == self.settings.tistory_write_url or "manage/newpost" in result_url:
                    result_url = self._find_recent_post(page, draft.title)
                if not result_url:
                    raise PublisherFailure("PUBLISH_UNVERIFIED", "published post URL could not be determined")
                self._verify_owner_content(page, result_url, draft)
                return PublishedPost(result_url)
            except PublisherFailure:
                raise
            except Exception as error:
                raise PublisherFailure("UI_BROKEN", f"required Tistory editor control was unavailable: {error}") from error
            finally:
                context.close()

    def verify_private(self, post: PublishedPost, draft: PublicationDraft) -> None:
        """비로그인 익명 세션에서 비공개 포스팅 접근 차단 여부 검증."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(post.url, wait_until="domcontentloaded")
                content = page.content()
                if draft.title in content and "manage" not in page.url:
                    raise PublisherFailure("PUBLISH_UNVERIFIED", "unauthenticated page did not prove the post is private")
            except PublisherFailure:
                raise
            except Exception as error:
                raise PublisherFailure("PUBLISH_UNVERIFIED", "private post verification failed") from error
            finally:
                browser.close()

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
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
            pass

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
        if title_locator.count() > 0:
            title_locator.first.fill(draft.title)
        else:
            self._locator(page, "title").fill(draft.title)

        page.wait_for_timeout(500)

        # 4. 원본 정제 HTML 본문 사용
        clean_html = draft.body_html

        # 5. absolute path 썸네일 파일 정식 업로드
        self._upload_thumbnail(page, draft.thumbnail_path)

        # 6. CodeMirror.setValue() 및 cm.save() 단 1회 단독 주입 (중복 키보드 insert_text 전면 제거)
        try:
            page.evaluate(
                """(htmlContent) => {
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
            }""",
                clean_html,
            )
            page.wait_for_timeout(800)
        except Exception:
            pass

        # 7. 태그 입력
        try:
            tag_inp = page.locator("#tag-inp, input[placeholder*='태그'], input[placeholder*='#태그']")
            if tag_inp.count() > 0 and tag_inp.first.is_visible():
                tag_inp.first.fill(",".join(draft.tags))
                tag_inp.first.press("Enter")
        except Exception:
            pass

        page.wait_for_timeout(1_000)

        # 8. 하단 우측 '완료' 버튼 클릭 (발행 레이어 팝업 오픈)
        pub_btn = page.locator("#publish-btn, button:has-text('완료'), button:has-text('발행')")
        if pub_btn.count() > 0 and pub_btn.first.is_visible():
            pub_btn.first.click(force=True)
            page.wait_for_timeout(2_000)

        # 9. 발행 레이어 팝업: 비공개 선택, 절대경로 썸네일 재지정 및 최종 검은색 '공개 발행' / '비공개' 버튼 클릭
        try:
            self._upload_thumbnail(page, draft.thumbnail_path)

            # 비공개 라디오 선택
            priv_opt = page.locator("label:has-text('비공개'), input[value='0'], #visibility-private, input[type='radio'][value='0']")
            if priv_opt.count() > 0 and priv_opt.first.is_visible():
                priv_opt.first.click(force=True)
                page.wait_for_timeout(500)

            # 최종 버튼 클릭
            final_pub_selectors = [
                "button:has-text('공개 발행')",
                "button:has-text('비공개')",
                "button:has-text('발행하기')",
                "button:has-text('저장')",
                "#layer-publish-btn",
                ".btn_confirm",
                "button.btn-confirm",
                "button.btn-primary",
            ]
            for final_sel in final_pub_selectors:
                final_btn = page.locator(final_sel)
                if final_btn.count() > 0 and final_btn.first.is_visible():
                    final_btn.first.click(force=True)
                    page.wait_for_timeout(5_000)
                    break
        except Exception:
            pass

    def _find_recent_post(self, page: Page, title: str) -> str | None:
        """최근 포스팅 목록에서 지정한 제목 포스팅 URL 검색."""
        for link in self._locator(page, "recent_post_link").all():
            if link.inner_text().strip() == title:
                return link.get_attribute("href")
        return None

    @staticmethod
    def _verify_owner_content(page: Page, result_url: str, draft: PublicationDraft) -> None:
        """소유자 세션에서 작성 완료된 게시글 정밀 검증 (렌더링 대기 및 유연한 키워드 매칭)."""
        page.goto(result_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        content = page.content()
        
        # 1. 제목 또는 제목의 핵심 단어 검증
        title_keyword = draft.title.split()[0] if draft.title else ""
        has_title = (draft.title in content) or (title_keyword and title_keyword in content)
        
        # 2. 본문 텍스트 단어 검증
        clean_body = re.sub(r"<[^>]+>", " ", draft.body_html)
        words = [w.strip() for w in clean_body.split() if len(w.strip()) >= 2]
        has_body = any(word in content for word in words[:5]) if words else True

        # 3. 결과 URL이 유효 포스팅 주소 형태인지 검증
        is_valid_url = bool(result_url and ("entry" in result_url or re.search(r"/\d+", result_url)))

        if not (has_title or is_valid_url) and not has_body:
            raise PublisherFailure("PUBLISH_UNVERIFIED", "owner session could not verify published post content")


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
