# 작업 계획서: 부정적 제약 완전 해제 및 고-CTR 어그로/바이럴 주제 수집 적용

## 1. 개요
프롬프트 내 부정적 제약(Negative Constraints)을 완전히 제거하여 주제 탐색 범위를 무제한으로 해제하고, 클릭률(CTR)과 조회수를 극대화할 수 있는 자극적이고 매력적인 **어그로/바이럴 헤드라인 주제**가 수집되도록 개선합니다.

## 2. 세부 변경 계획

### 백엔드 (Backend)
1. **`app/topics/discovery.py`**:
   - 부정적 제한 문구(`DO NOT`, `EXCLUDE` 등)를 완전히 해제/제거.
   - 클릭 유도 및 호기심 자극 지시어 추가:
     - "High-CTR, viral, clickworthy Korean blog topics"
     - "Craft highly engaging and catchy topic titles that compel readers to click" (예: 모르면 손해보는 혜택, 8월부터 달라지는 규제, 충격적인 숨은 팁 등)

### 테스트 및 검증 (Tests)
1. `tests/test_topic_discovery.py`: 어그로/바이럴 클릭 유도 프롬프트 단서문구 검증 적용.
2. pytest 전체 테스트 실행 및 검증.

## 3. 진행 현황
- [x] 작업 계획 수립 및 마크다운 문서 작성
- [x] `discovery.py` 부정적 제약 완전 해제 및 바이럴 어그로 프롬프트 적용
- [x] 테스트 코드 업데이트 및 pytest 전체 검증 완료 (43개 통과)
