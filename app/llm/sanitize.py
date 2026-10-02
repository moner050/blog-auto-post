"""LLM이 만든 본문 HTML을 허용 목록 기반으로 정제한다.

모델은 웹 검색 결과를 읽으므로(간접 프롬프트 주입) 출력 HTML을 신뢰하지 않는다.
입력을 그대로 고치지 않고, 허용한 태그와 속성만 새로 직렬화한다. 링크는 LinkPolicy를 통과한 것만 남기고,
표에는 어떤 블로그 스킨에서도 읽히는 인라인 스타일을 입힌다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import re

from app.llm.sources import LinkPolicy, clean_url, link_rel
from app.llm.textutil import strip_invisible

# 마크다운 변환·검색 결과에서 딸려 오는 출처 번호([1], [1,2], [1-3])
CITATION_MARKER = re.compile(r"[ \t]?\[\d{1,3}(?:\s*[,~\-–]\s*\d{1,3})*\]")

_ALLOWED_TAGS = frozenset(
    {
        "h2", "h3", "p", "br", "hr", "ul", "ol", "li", "strong", "em", "table", "thead", "tbody", "tfoot",
        "tr", "th", "td", "caption", "blockquote", "a", "code", "pre",
    }
)
_ALIASES = {"b": "strong", "i": "em", "h1": "h2", "h4": "h3", "h5": "h3", "h6": "h3"}
# 실제 HTML 요소 이름. 이 밖의 이름(List<String>의 <String>, <T>, <stdio.h> 등)은 태그가 아니라 글자이므로 지우지 않고 남긴다.
_HTML_ELEMENTS = frozenset(
    """a abbr acronym address applet area article aside audio b base basefont bdi bdo big blink blockquote body br button
    canvas caption center cite code col colgroup data datalist dd del details dfn dialog dir div dl dt em embed fieldset
    figcaption figure font footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins
    kbd label legend li link main map mark marquee math menu meta meter nav nobr noembed noframes noscript object ol
    optgroup option output p param picture plaintext pre progress q rp rt ruby s samp script search section select slot
    small source span strike strong style sub summary sup svg table tbody td template textarea tfoot th thead time title
    tr track tt u ul var video wbr xmp""".split()
)
_VOID_TAGS = frozenset({"br", "hr"})
# 내용까지 통째로 버리는 태그
_DROP_WITH_CONTENT = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math", "head", "title",
        "textarea", "select", "option", "button", "video", "audio", "canvas", "applet", "frameset", "frame",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "h2", "h3", "p", "br", "hr", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
        "caption", "blockquote", "pre",
    }
)
_NEWLINE_AFTER = frozenset({"p", "li", "h2", "h3", "ul", "ol", "tr", "table", "blockquote", "pre", "br", "hr"})
_TABLE_PARTS = frozenset({"thead", "tbody", "tfoot", "tr", "th", "td", "caption"})
# 새 태그를 열기 전에 먼저 닫아야 하는 열린 태그(HTML 파서의 암묵적 종료 규칙)
_AUTO_CLOSE = {
    "li": {"li", "p"},
    "p": {"p"},
    "a": {"a"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "thead": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "h2": {"p"},
    "h3": {"p"},
    "ul": {"p"},
    "ol": {"p"},
    "table": {"p"},
    "blockquote": {"p"},
    "pre": {"p"},
    "hr": {"p"},
}
# 이 태그를 열기 전에, 인라인 태그만 사이에 낀 열린 <p>를 닫는다(HTML 파서의 "close a p element" 규칙)
_CLOSES_PARAGRAPH = frozenset({"p", "h2", "h3", "ul", "ol", "table", "blockquote", "pre", "hr"})
# 한 줄짜리 <code> 안에는 올 수 없는 블록 태그(닫지 않은 <code>를 닫는 신호로 쓴다)
_BLOCK_STARTS = _CLOSES_PARAGRAPH | {"li", "tr", "td", "th", "thead", "tbody", "tfoot", "caption"}
_INLINE_TAGS = frozenset({"strong", "em", "a", "code"})
_LANGUAGE_CLASS = re.compile(r"^language-[A-Za-z0-9_+#.\-]{1,20}$")
_SMALL_INT = re.compile(r"^\d{1,3}$")
_SPAN = re.compile(r"^(?:[1-9]|10)$")  # colspan·rowspan. 표를 망가뜨릴 만큼 큰 값은 받지 않는다
# LLM 출력은 토큰 수로 제한되지만, 최악의 입력에서도 처리 시간이 폭주하지 않도록 상한을 둔다.
MAX_INPUT_CHARS = 400_000
MAX_NESTING = 60

# 어두운 스킨에서도 읽히도록 반투명 회색을 쓴다.
_TABLE_WRAP_STYLE = "overflow-x:auto;"
_TABLE_STYLE = "width:100%;border-collapse:collapse;margin:12px 0;"
_CELL_STYLE = "border:1px solid rgba(127,127,127,0.45);padding:8px 10px;text-align:left;vertical-align:top;"
_HEAD_CELL_STYLE = _CELL_STYLE + "background:rgba(127,127,127,0.12);font-weight:600;"


@dataclass(frozen=True)
class SanitizedHtml:
    html: str
    text: str
    h2_count: int
    table_count: int
    ol_count: int
    removed_links: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# '[^<>]'로 시작 '<'마다 다음 '<'까지만 훑게 해, '>' 없이 '<'가 반복되는 입력에서도 선형 시간으로 끝난다.
_TAG_RE = re.compile(r"<[^<>]*>")


def strip_tags(text: str, replacement: str = "") -> str:
    """태그처럼 보이는 토막을 지운다(제목·요약·태그 같은 짧은 텍스트 필드용)."""
    return _TAG_RE.sub(replacement, text)


def strip_citation_markers(text: str) -> str:
    return CITATION_MARKER.sub("", text)


def strip_presentation(html: str) -> str:
    """sanitize_html이 입힌 표 스타일·스크롤 래퍼를 걷어낸다(재작성 요청에 불필요한 토큰을 싣지 않기 위함)."""
    html = re.sub(r' style="[^"]*"', "", html)
    return html.replace("<div>", "").replace("</div>", "")


def sanitize_html(raw_html: str, *, title: str = "", policy: LinkPolicy | None = None) -> SanitizedHtml:
    """본문 HTML을 정제한다. 파싱에 실패하면 텍스트만 문단으로 감싸 돌려준다."""
    sanitizer = _Sanitizer(title=title, policy=policy or LinkPolicy())
    if len(raw_html) > MAX_INPUT_CHARS:
        raw_html = raw_html[:MAX_INPUT_CHARS]
        sanitizer.notes.append("본문이 너무 길어 일부를 잘랐습니다.")
    try:
        sanitizer.feed(raw_html)
        sanitizer.close()
    except Exception:
        plain = "\n".join(f"<p>{escape(line.strip(), quote=False)}</p>" for line in strip_tags(raw_html, " ").splitlines() if line.strip())
        text = re.sub(r"\s+", " ", strip_tags(plain, " ")).strip()
        return SanitizedHtml(html=plain, text=text, h2_count=0, table_count=0, ol_count=0, notes=["본문 HTML 구조가 깨져 텍스트만 남겼습니다."])
    return sanitizer.result()


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


class _Sanitizer(HTMLParser):
    def __init__(self, *, title: str, policy: LinkPolicy) -> None:
        super().__init__(convert_charrefs=True)
        self.policy = policy
        self.title_key = _compact(title)
        self.out: list[str] = []
        self.text: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (태그, 출력 여부) — 허용하지 않은 링크는 출력하지 않는다
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.pre_depth = 0
        self.code_depth = 0
        self.heading: tuple[int, int, bool] | None = None  # (out 위치, text 위치, 원래 h1 여부)
        self.h2_count = 0
        self.table_count = 0
        self.ol_count = 0
        self.removed_links: list[str] = []
        self.notes: list[str] = []
        self.literal_tags: list[str] = []

    # ---- 결과 ----
    def result(self) -> SanitizedHtml:
        if self._in_code():
            self.notes.append("코드 블록(<pre>·<code>)이 닫히지 않아 이후 내용이 코드(글자)로 처리됐습니다.")
        if self.literal_tags:
            self.notes.append(
                f"HTML 요소가 아닌 꺾쇠 표기 {len(self.literal_tags)}개({', '.join(self.literal_tags[:3])})를 글자로 남겼습니다. "
                "태그가 아니라 의도한 표기인지 확인하세요."
            )
        while self.stack:
            self._close_top()
        html = re.sub(r"\n{3,}", "\n\n", "".join(self.out)).strip()
        text = re.sub(r"\s+", " ", "".join(self.text)).strip()
        return SanitizedHtml(
            html=html,
            text=text,
            h2_count=self.h2_count,
            table_count=self.table_count,
            ol_count=self.ol_count,
            removed_links=self.removed_links,
            notes=self.notes,
        )

    # ---- HTMLParser 콜백 ----
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.skip_tag and self._is_literal(tag.lower()):
            self._literal_start(tag.lower())
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if self.code_depth and not self.pre_depth and tag in _BLOCK_STARTS:
            self._close_inline_code()  # 본문 속 <code>를 닫지 않은 채 문단·목록이 이어진 경우: 뒤 내용이 전부 글자로 바뀌지 않게 닫는다
        if self._is_literal(tag):
            self._literal_start(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
            return
        original = tag
        tag = _ALIASES.get(tag, tag)
        if tag not in _ALLOWED_TAGS:
            return  # 알 수 없는 태그는 벗기고 안의 텍스트만 남긴다
        if tag == "li" and not self._inside("ul", "ol"):
            return
        if tag in _TABLE_PARTS and not self._inside("table"):
            return

        if tag in _CLOSES_PARAGRAPH:
            self._close_open_paragraph()
        closers = _AUTO_CLOSE.get(tag, ())
        while closers and self.stack and self.stack[-1][0] in closers:
            self._close_top()
        if len(self.stack) >= MAX_NESTING:
            return  # 비정상적으로 깊은 중첩은 벗기고 텍스트만 남긴다
        if tag in _BLOCK_TAGS:
            self.text.append(" ")

        if tag in ("h2", "h3"):
            self.heading = (len(self.out), len(self.text), original == "h1")
        if tag == "h2":
            self.h2_count += 1
        elif tag == "ol":
            self.ol_count += 1
        elif tag == "table":
            self.table_count += 1
            self.out.append(f'<div style="{_TABLE_WRAP_STYLE}">')
        elif tag == "pre":
            self.pre_depth += 1
        elif tag == "code":
            self.code_depth += 1

        emitted = True
        if tag == "a":
            emitted = self._open_link(dict(attrs))
        else:
            self.out.append(self._open_tag(tag, dict(attrs)))
        if tag not in _VOID_TAGS:
            self.stack.append((tag, emitted))
        elif tag in _NEWLINE_AFTER:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if self._is_literal(tag):
            if tag != "br":
                self.handle_data(f"</{tag}>")
            return
        tag = _ALIASES.get(tag, tag)
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if not any(name == tag for name, _ in self.stack):
            return  # 짝 없는 닫는 태그
        while self.stack:
            closed = self.stack[-1][0]
            self._close_top()
            if closed == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip_tag:
            return
        data = strip_invisible(data)
        if not self._in_code():
            data = strip_citation_markers(data)
        if not data:
            return
        self.out.append(escape(data, quote=False))
        self.text.append(data)

    # 코드 샘플 안의 주석·선언(<!-- -->, <!DOCTYPE>, <?xml ?>, <![CDATA[ ]]>)은 버리지 않고 글자로 남긴다.
    def handle_comment(self, data: str) -> None:
        if self._in_code():
            self.handle_data(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if self._in_code():
            self.handle_data(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if self._in_code():
            self.handle_data(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        if self._in_code():
            self.handle_data(f"<![{data}]>")

    # ---- 내부 ----
    def _in_code(self) -> bool:
        return self.pre_depth > 0 or self.code_depth > 0

    def _is_literal(self, tag: str) -> bool:
        """이 태그를 태그가 아니라 글자로 남겨야 하는가: 코드 안의 태그(예시 코드의 마크업), 또는 HTML 요소가 아닌 이름(제네릭 등)."""
        if self._in_code():
            return tag not in ("pre", "code")
        return tag not in _HTML_ELEMENTS

    def _literal_start(self, tag: str) -> None:
        if tag == "br" and self._in_code():
            self.handle_data("\n")
            return
        raw = self.get_starttag_text() or ""
        if not self._in_code():
            self.literal_tags.append(raw[:30])  # 코드 밖의 꺾쇠 표기는 의도한 것인지 사람이 확인하도록 알린다
        self.handle_data(raw)

    def _close_inline_code(self) -> None:
        while self.stack and self.code_depth:
            self._close_top()

    def _inside(self, *names: str) -> bool:
        return any(name in names for name, _ in self.stack)

    def _close_open_paragraph(self) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            name = self.stack[index][0]
            if name == "p":
                while len(self.stack) > index:
                    self._close_top()
                return
            if name not in _INLINE_TAGS:
                return

    def _close_top(self) -> None:
        tag, emitted = self.stack.pop()
        if tag in ("h2", "h3") and self.heading is not None:
            out_pos, text_pos, was_h1 = self.heading
            self.heading = None
            if was_h1 and _compact("".join(self.text[text_pos:])) == self.title_key and self.title_key:
                del self.out[out_pos:]
                del self.text[text_pos:]
                self.h2_count -= 1
                self.notes.append("본문 첫머리에 반복된 제목(h1)을 제거했습니다.")
                return
        if tag == "pre":
            self.pre_depth = max(0, self.pre_depth - 1)
        elif tag == "code":
            self.code_depth = max(0, self.code_depth - 1)
        if emitted:
            self.out.append(f"</{tag}>" + ("</div>" if tag == "table" else "") + ("\n" if tag in _NEWLINE_AFTER else ""))
        if tag in _BLOCK_TAGS:
            self.text.append(" ")

    def _open_link(self, attrs: dict[str, str | None]) -> bool:
        href = attrs.get("href")
        url = clean_url(href) if href else None
        if url is not None and self.policy.allows(url):
            self.out.append(f'<a href="{escape(url, quote=True)}" target="_blank" rel="{link_rel(url)}">')
            return True
        if href:
            self.removed_links.append(href[:200])
        return False

    @staticmethod
    def _open_tag(tag: str, attrs: dict[str, str | None]) -> str:
        extra = ""
        if tag == "table":
            extra = f' style="{_TABLE_STYLE}"'
        elif tag in ("th", "td"):
            extra = f' style="{_HEAD_CELL_STYLE if tag == "th" else _CELL_STYLE}"'
            for name in ("colspan", "rowspan"):
                value = (attrs.get(name) or "").strip()
                if _SPAN.match(value):
                    extra += f' {name}="{value}"'
            scope = (attrs.get("scope") or "").strip().lower()
            if tag == "th" and scope in ("col", "row"):
                extra += f' scope="{scope}"'
        elif tag == "ol":
            value = (attrs.get("start") or "").strip()
            if _SMALL_INT.match(value):
                extra = f' start="{value}"'
        elif tag == "code":
            value = (attrs.get("class") or "").strip()
            if _LANGUAGE_CLASS.match(value):
                extra = f' class="{value}"'
        return f"<{tag}{extra}>"
