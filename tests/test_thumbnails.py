from __future__ import annotations

from pathlib import Path

from app.content.thumbnails import get_thumbnail_for_category


def test_category_thumbnail_mapping():
    # 1. 생활꿀팁
    life_thumb = get_thumbnail_for_category("생활꿀팁")
    assert life_thumb == Path("storage/lifestyle.png")

    # 2. 정부지원·민원
    gov_thumb = get_thumbnail_for_category("정부지원·민원")
    assert gov_thumb == Path("storage/government-support.png")

    # 3. 대출·금융
    loan_thumb = get_thumbnail_for_category("대출·금융")
    assert loan_thumb == Path("storage/loan-finance.png")

    # 4. 세금·환급
    tax_thumb = get_thumbnail_for_category("세금·환급")
    assert tax_thumb == Path("storage/tax-refund.png")

    # 5. 교통·카드혜택
    traffic_thumb = get_thumbnail_for_category("교통·카드혜택")
    assert traffic_thumb == Path("storage/traffic-card.png")

    # 6. 여행·할인
    travel_thumb = get_thumbnail_for_category("여행·할인")
    assert travel_thumb == Path("storage/travel-discount.png")

    # 7. 기타 또는 미지정 카테고리 (기본 썸네일 반환)
    unknown_thumb = get_thumbnail_for_category("기타카테고리")
    assert unknown_thumb == Path("storage/default_thumbnail.png")
