from __future__ import annotations

from pathlib import Path

from app.content.thumbnails import get_thumbnail_for_category


def test_category_thumbnail_mapping():
    # 1. 법률·합의·분쟁
    legal_thumb = get_thumbnail_for_category("법률·합의·분쟁")
    assert legal_thumb == Path("storage/legal-dispute.png")

    # 2. 대출·부채·금융
    loan_thumb = get_thumbnail_for_category("대출·부채·금융")
    assert loan_thumb == Path("storage/loan-finance.png")

    # 3. 보험·보상·청구
    insurance_thumb = get_thumbnail_for_category("보험·보상·청구")
    assert insurance_thumb == Path("storage/insurance-claim.png")

    # 4. 세금·환급·절세
    tax_thumb = get_thumbnail_for_category("세금·환급·절세")
    assert tax_thumb == Path("storage/tax-refund.png")

    # 5. 주식·코인·투자
    stock_thumb = get_thumbnail_for_category("주식·코인·투자")
    assert stock_thumb == Path("storage/stock-crypto.png")

    # 6. 차량·리스·렌트
    car_thumb = get_thumbnail_for_category("차량·리스·렌트")
    assert car_thumb == Path("storage/car-rental.png")

    # 7. 여행·특가·예약
    travel_thumb = get_thumbnail_for_category("여행·특가·예약")
    assert travel_thumb == Path("storage/travel-discount.png")

    # 8. 키워드 부분 일치 검증
    assert get_thumbnail_for_category("비트코인 반감기 전망") == Path("storage/stock-crypto.png")
    assert get_thumbnail_for_category("교통사고 형사합의 가이드") == Path("storage/legal-dispute.png")
    assert get_thumbnail_for_category("법원 판결문 열람 방법") == Path("storage/legal-dispute.png")
    assert get_thumbnail_for_category("실손보험 도수치료 청구") == Path("storage/insurance-claim.png")
    assert get_thumbnail_for_category("연말정산 환급금 조회") == Path("storage/tax-refund.png")
    assert get_thumbnail_for_category("중고차 침수차 구별법") == Path("storage/car-rental.png")

    # 9. 기타 또는 미지정 카테고리 (기본 썸네일 반환)
    unknown_thumb = get_thumbnail_for_category("기타카테고리")
    assert unknown_thumb == Path("storage/default_thumbnail.png")
