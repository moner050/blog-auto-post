# 작업 계획서: 수집 기간 필터 1주일(week) 변경 (Topic Discovery Error 방지)

## 1. 개요
하루(`day`) 필터 적용 시 특정 시간대에 Sonar 검색 인덱스가 유효한 출처(Citations) 및 카테고리 후보 조건을 다 충족하지 못해 `Sonar returned no valid topic candidates` 에러가 발생하는 문제를 해결합니다. 수집 최신성 필터를 **1주일(`week`)**로 변경하여 안정적이고 풍부한 주제 수집을 보장합니다.

## 2. 세부 변경 계획

### 백엔드 (Backend)
1. **`app/topics/discovery.py`**:
   - `search_recency_filter="week"`로 변경.
   - 프롬프트 지시어 내 시의성 문구를 `from the last 7 days as of {current_date_str}`로 업데이트.

### 테스트 및 검증 (Tests)
1. `tests/test_topic_discovery.py`: `search_recency_filter == "week"` 검증 테스트 반영.
2. pytest 전체 테스트 실행 및 검증 완료.

## 3. 진행 현황
- [x] 작업 계획 수립 및 마크다운 문서 작성
- [x] `discovery.py` `search_recency_filter="week"` 반영
- [x] 테스트 코드 업데이트 및 pytest 전체 검증 완료 (43개 통과)
