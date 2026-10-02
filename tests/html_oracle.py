"""sanitize_html 출력이 허용한 문법만 쓰는지 검사하는 독립 오라클.

블랙리스트(금지 문자열 검색)는 새 공격 형태를 모르면 놓치므로, 출력에 나올 수 있는 모든 태그 모양을 허용 목록으로
적어 두고 거기에 맞지 않는 것은 전부 위반으로 본다. 구현의 상수를 가져오지 않고 일부러 따로 적었다(구현이 틀려도
같이 틀리지 않게).
"""

from __future__ import annotations

import html as html_lib
import re

from app.llm.sources import clean_url

_TABLE_STYLE = "width:100%;border-collapse:collapse;margin:12px 0;"
_CELL_STYLE = "border:1px solid rgba(127,127,127,0.45);padding:8px 10px;text-align:left;vertical-align:top;"
_HEAD_STYLE = _CELL_STYLE + "background:rgba(127,127,127,0.12);font-weight:600;"
_TAG = re.compile(
    r"""
    </(?:h2|h3|p|ul|ol|li|strong|em|table|thead|tbody|tfoot|tr|th|td|caption|blockquote|a|code|pre|div)>
  | <(?:h2|h3|p|br|hr|ul|li|strong|em|thead|tbody|tfoot|tr|caption|blockquote|pre)>
  | <ol(?:\ start="\d{1,3}")?>
  | <code(?:\ class="language-[A-Za-z0-9_+\#.\-]{1,20}")?>
  | <div\ style="overflow-x:auto;">
  | <table\ style="%s">
  | <(?:td|th)\ style="(?:%s|%s)"(?:\ colspan="(?:[1-9]|10)")?(?:\ rowspan="(?:[1-9]|10)")?(?:\ scope="(?:col|row)")?>
  | <a\ href="(?P<href>[^"<>']*)"\ target="_blank"\ rel="noopener\ noreferrer(?:\ nofollow)?">
    """
    % (re.escape(_TABLE_STYLE), re.escape(_CELL_STYLE), re.escape(_HEAD_STYLE)),
    re.VERBOSE,
)
_BAD_TEXT = re.compile(r"[<>]|&(?!amp;|lt;|gt;)")


def grammar_violations(output: str) -> list[str]:
    """허용 문법을 벗어난 부분의 목록(비어 있으면 통과)."""
    problems: list[str] = []
    position = 0
    while position < len(output):
        opening = output.find("<", position)
        text_end = len(output) if opening == -1 else opening
        chunk = output[position:text_end]
        if _BAD_TEXT.search(chunk):
            problems.append(f"허용되지 않는 글자 조각: {chunk[:80]!r}")
        if opening == -1:
            break
        match = _TAG.match(output, opening)
        if match is None:
            problems.append(f"허용 문법에 없는 태그: {output[opening:opening + 80]!r}")
            position = opening + 1
            continue
        href = match.groupdict().get("href")
        if href is not None:
            raw = html_lib.unescape(href)
            if clean_url(raw) != raw or not re.match(r"^https?://", raw, re.IGNORECASE):
                problems.append(f"깨끗하지 않은 href: {raw!r}")
            if re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", href):
                problems.append(f"이스케이프되지 않은 &가 있는 href: {href!r}")
        position = match.end()
    return problems
