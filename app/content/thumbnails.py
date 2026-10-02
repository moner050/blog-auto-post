from __future__ import annotations

from pathlib import Path

# 카테고리별 고정 썸네일 매핑 테이블 (고수익 7대 카테고리)
CATEGORY_THUMBNAIL_MAP: dict[str, Path] = {
    "법률·합의·분쟁": Path("storage/legal-dispute.png"),
    "대출·부채·금융": Path("storage/loan-finance.png"),
    "보험·보상·청구": Path("storage/insurance-claim.png"),
    "세금·환급·절세": Path("storage/tax-refund.png"),
    "주식·코인·투자": Path("storage/stock-crypto.png"),
    "차량·리스·렌트": Path("storage/car-rental.png"),
    "여행·특가·예약": Path("storage/travel-discount.png"),
}

# 하위 호환 및 부분 매칭용 키워드 테이블
KEYWORD_THUMBNAIL_MAP: dict[str, Path] = {
    "법률": Path("storage/legal-dispute.png"),
    "합의": Path("storage/legal-dispute.png"),
    "분쟁": Path("storage/legal-dispute.png"),
    "변호사": Path("storage/legal-dispute.png"),
    "소송": Path("storage/legal-dispute.png"),
    "판례": Path("storage/legal-dispute.png"),
    "법원": Path("storage/legal-dispute.png"),
    "대출": Path("storage/loan-finance.png"),
    "부채": Path("storage/loan-finance.png"),
    "금융": Path("storage/loan-finance.png"),
    "금리": Path("storage/loan-finance.png"),
    "이자": Path("storage/loan-finance.png"),
    "보험": Path("storage/insurance-claim.png"),
    "보상": Path("storage/insurance-claim.png"),
    "청구": Path("storage/insurance-claim.png"),
    "실손": Path("storage/insurance-claim.png"),
    "과실": Path("storage/insurance-claim.png"),
    "세금": Path("storage/tax-refund.png"),
    "환급": Path("storage/tax-refund.png"),
    "절세": Path("storage/tax-refund.png"),
    "연말정산": Path("storage/tax-refund.png"),
    "주식": Path("storage/stock-crypto.png"),
    "코인": Path("storage/stock-crypto.png"),
    "투자": Path("storage/stock-crypto.png"),
    "비트코인": Path("storage/stock-crypto.png"),
    "가상화폐": Path("storage/stock-crypto.png"),
    "암호화폐": Path("storage/stock-crypto.png"),
    "증권": Path("storage/stock-crypto.png"),
    "ETF": Path("storage/stock-crypto.png"),
    "차량": Path("storage/car-rental.png"),
    "리스": Path("storage/car-rental.png"),
    "렌트": Path("storage/car-rental.png"),
    "중고차": Path("storage/car-rental.png"),
    "자동차": Path("storage/car-rental.png"),
    "여행": Path("storage/travel-discount.png"),
    "특가": Path("storage/travel-discount.png"),
    "예약": Path("storage/travel-discount.png"),
    "항공": Path("storage/travel-discount.png"),
    "호텔": Path("storage/travel-discount.png"),
}


def get_thumbnail_for_category(category: str | None, custom_thumbnail: Path | str | None = None) -> Path:
    """카테고리에 대응하는 고정 썸네일 경로 반환 (직접 지정 시 우선 적용)."""
    if custom_thumbnail:
        path = Path(custom_thumbnail) if isinstance(custom_thumbnail, str) else custom_thumbnail
        if path.is_file():
            return path

    if category:
        clean_category = category.strip()
        # 1. 완전 일치 탐색
        if clean_category in CATEGORY_THUMBNAIL_MAP:
            mapped_path = CATEGORY_THUMBNAIL_MAP[clean_category]
            if mapped_path.is_file():
                return mapped_path

        # 2. 카테고리명 부분 일치 탐색
        for key, path in CATEGORY_THUMBNAIL_MAP.items():
            if key in clean_category and path.is_file():
                return path

        # 3. 핵심 키워드 매칭 탐색
        for keyword, path in KEYWORD_THUMBNAIL_MAP.items():
            if keyword in clean_category and path.is_file():
                return path

    # 기본 썸네일 폴백 및 생성
    default_path = Path("storage/default_thumbnail.png")
    default_path.parent.mkdir(parents=True, exist_ok=True)
    if not default_path.is_file():
        default_path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xafA4\x00\x00\x00\x00IEND\aeB`"
        )
    return default_path
