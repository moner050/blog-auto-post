"""모델 출력과 검색 결과에서 온 텍스트를 다룰 때 쓰는 작은 도구."""

from __future__ import annotations

import re

# 보이지 않는데 글을 바꾸는 문자의 코드 포인트 범위: 제어문자(탭·줄바꿈 제외), 서식 문자(제로폭 공백, 방향 제어, BOM,
# 프롬프트에 글을 숨기는 태그 문자 U+E0000대), 사설 영역, 단독 서로게이트. 이모지를 잇는 ZWJ(U+200D)와
# 변형 선택자(U+FE0F)는 남긴다.
_INVISIBLE_RANGES = (
    (0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x061C, 0x061C), (0x180E, 0x180E),
    (0x200B, 0x200C), (0x200E, 0x200F), (0x2028, 0x202E), (0x2060, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB),
    (0xD800, 0xDFFF), (0xE000, 0xF8FF), (0xE0000, 0xE007F), (0xF0000, 0x10FFFF),
)
_INVISIBLE = re.compile(
    "[" + "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in _INVISIBLE_RANGES) + "]"
)


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text)
