from __future__ import annotations

from pathlib import Path

from app.content.thumbnails import get_thumbnail_for_category


def test_category_thumbnail_mapping():
    # 1. 홈텍스 / 홈택스
    hometax_thumb = get_thumbnail_for_category("홈텍스")
    assert hometax_thumb == Path("storage/hometax.png")

    hometax_alt = get_thumbnail_for_category("홈택스")
    assert hometax_alt == Path("storage/hometax.png")

    # 2. 정부정책
    policy_thumb = get_thumbnail_for_category("정부정책")
    assert policy_thumb == Path("storage/goverment-policy.png")

    # 3. 정부24
    gov24_thumb = get_thumbnail_for_category("정부24")
    assert gov24_thumb == Path("storage/goverment24.png")

    # 4. 법원 / 대법원 / 법률
    law_thumb = get_thumbnail_for_category("법원")
    assert law_thumb == Path("storage/law.png")

    # 5. 생활꿀팁
    life_thumb = get_thumbnail_for_category("생활꿀팁")
    assert life_thumb == Path("storage/lifestyle.png")

    # 6. 여행꿀팁 / 여행
    travel_thumb = get_thumbnail_for_category("여행꿀팁")
    assert travel_thumb == Path("storage/travel.png")

    travel_alt = get_thumbnail_for_category("여행")
    assert travel_alt == Path("storage/travel.png")

    # 7. 미지정 또는 기타 카테고리 (기본 썸네일 반환)
    unknown_thumb = get_thumbnail_for_category("기타카테고리")
    assert unknown_thumb == Path("storage/default_thumbnail.png")
