from __future__ import annotations

from pathlib import Path

# 카테고리별 고정 썸네일 매핑 테이블 (정부24, 법원, 여행꿀팁 추가)
CATEGORY_THUMBNAIL_MAP: dict[str, Path] = {
    "홈텍스": Path("storage/hometax.png"),
    "홈택스": Path("storage/hometax.png"),
    "정부정책": Path("storage/goverment-policy.png"),
    "정부24": Path("storage/goverment24.png"),
    "법원": Path("storage/law.png"),
    "대법원": Path("storage/law.png"),
    "법률": Path("storage/law.png"),
    "생활꿀팁": Path("storage/lifestyle.png"),
    "여행꿀팁": Path("storage/travel.png"),
    "여행": Path("storage/travel.png"),
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

        # 2. 키워드 부분 일치 탐색 (예: '정부24 발급' -> goverment24.png)
        for key, path in CATEGORY_THUMBNAIL_MAP.items():
            if key in clean_category and path.is_file():
                return path

    # 기본 썸네일 폴백 및 생성
    default_path = Path("storage/default_thumbnail.png")
    default_path.parent.mkdir(parents=True, exist_ok=True)
    if not default_path.is_file():
        default_path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xafA4\x00\x00\x00\x00IEND\aeB`"
        )
    return default_path
