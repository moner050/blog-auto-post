# 작업 계획서: 블로그 카테고리 전면 개편 및 썸네일 매칭 / UI / AI 주제 추천 수정

## 1. 작업 개요
블로그 카테고리 전면 개편에 따라 기존의 모든 썸네일 매칭을 초기화하고 아래 6개 신규 카테고리에 맞는 썸네일 매칭, 웹 대시보드 AI 포스팅 생성 카테고리 셀렉트 박스 전환, AI 주제 추천 카테고리 및 프롬프트 업데이트를 수행합니다.

## 2. 신규 카테고리 및 썸네일 매칭 상세
1. **생활꿀팁**: `storage/lifestyle.png`
2. **정부지원·민원**: `storage/government-support.png`
3. **대출·금융**: `storage/loan-finance.png`
4. **세금·환급**: `storage/tax-refund.png`
5. **교통·카드혜택**: `storage/traffic-card.png`
6. **여행·할인**: `storage/travel-discount.png`

## 3. 세부 작업 항목
1. **`app/content/thumbnails.py`**:
   - `CATEGORY_THUMBNAIL_MAP` 초기화 및 신규 6개 매칭 적용
2. **`app/topics/discovery.py`**:
   - `ALLOWED_CATEGORIES` 신규 6개 카테고리로 변경
   - Perplexity Sonar 주제 추천 시스템 및 사용자 프롬프트 갱신
3. **`app/web/templates/index.html`**:
   - `input-category` 텍스트 입력창을 `<select>` 카테고리 드롭다운으로 개편
4. **`.gitignore`**:
   - `storage/` 하위 신규 썸네일 PNG 6종 무시 예외 추가
5. **테스트 케이스 업데이트 (`tests/test_thumbnails.py`, `tests/test_topic_discovery.py`)**:
   - 신규 카테고리 검증 테스트로 전면 개편 및 `pytest` 검증
