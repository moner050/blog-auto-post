# 작업 계획서: AI 주제 추천 완료 상태 UI 명시적 표기 기능 개선

## 1. 개요
사용자가 AI 주제 추천 수집 요청 후 완결 여부를 직관적으로 인지할 수 있도록, 수집 중/완료/실패 상태를 시각적 애니메이션, 성공 토스트 알림, 최근 수집 타임스탬프 뱃지, 스무스 스크롤 및 버튼 반향 효과로 명확히 표기합니다.

## 2. 세부 변경 계획

### 프론트엔드 HTML / CSS / JS
1. **`app/web/templates/index.html`**:
   - AI 주제 추천 섹션 내 최근 수집 시각 및 상태를 알려주는 타임스탬프 뱃지 타겟 (`<span id="topic-discovery-status-badge"></span>`) 추가.
2. **`app/web/static/css/style.css`**:
   - 완료 메시지 상자 강조 애니메이션 (`@keyframes pulse-success`, `.alert-message.pulse`) 추가.
   - 수집 완료 상태 버튼 성공 하이라이트 클래스 (`.btn-success-glow`) 추가.
   - 수집 완료 타임스탬프 뱃지 스타일 추가.
3. **`app/web/static/js/main.js`**:
   - `discoverTopics()` 완료 시:
     - 버튼 텍스트를 `✅ 12개 주제 수집 완료!`로 2.5초간 변경 및 초록색 강조 후 원래대로 복원.
     - 알림 메시지 영역에 스무스 스크롤 (`scrollIntoView`) 및 펄스 효과 적용.
     - 최근 수집 시각 타임스탬프(예: `마지막 수집: 오후 2:35분`) 뱃지 업데이트.

### 테스트 및 검증
1. `tests/test_web.py`: 기존 수집 API 및 대시보드 페이지 렌더링 검증.
2. pytest 전체 검증.

## 3. 진행 현황
- [x] 작업 계획 수립 및 마크다운 문서 작성
- [x] HTML 템플릿 타임스탬프 뱃지 추가 (`index.html`)
- [x] CSS 성공 강조 애니메이션 및 뱃지 스타일 작성 (`style.css`)
- [x] JS 완료 반향 및 스무스 스크롤, 버튼 전환 로직 작성 (`main.js`)
- [x] 전체 검증 완료 (43개 테스트 통과)
