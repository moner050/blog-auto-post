# 작업 계획서: 하루(24시간) 기준 실시간 핫이슈 수집 기능 반영

## 1. 개요
사용자 요청에 따라 Perplexity Sonar 웹 검색 최신성 필터를 기존 `week`(최근 7일)에서 **`day`(최근 24시간 / 하루)**로 변경합니다. 이를 통해 오늘 당장 가장 뜨겁고 트렌디한 실시간 이슈 및 핫한 꿀팁 주제를 수집하도록 개선합니다.

## 2. 세부 변경 계획

### 백엔드 (Backend)
1. **`app/topics/discovery.py`**:
   - `search_recency_filter="day"` 적용 (최근 24시간 이내 웹 정보만 검색).
   - 프롬프트 지시어 내 시의성 문구를 `from the last 24 hours as of {current_date_str}`로 업데이트.

### 테스트 및 검증 (Tests)
1. `tests/test_topic_discovery.py`: `search_recency_filter == "day"` 검증 테스트 반영.
2. pytest 전체 테스트 실행 및 검증.

## 3. 진행 현황
- [x] 작업 계획 수립 및 마크다운 문서 작성
- [x] `discovery.py` `search_recency_filter="day"` 및 24시간 프롬프트 반영
- [x] 테스트 코드 업데이트 및 pytest 전체 검증 완료 (43개 통과)
