# 작업 계획서: 커뮤니티&SNS 전용 AI 주제 추천 옵션 개선

## 1. 개요
사용자 요청에 따라 "커뮤니티&SNS" 체크박스 체크 시 단순히 비중을 높이는 것이 아니라 **오직 커뮤니티 및 SNS(디시인사이드, 클리앙, 뽐뿌, 루리웹, 에펨코리아, 네이버 카페, 블라인드, 인스타그램, 트위터/X, 유튜브 등)에서만** 주제를 선정하도록 프롬프트 지시어와 UI 텍스트를 변경합니다.

## 2. 세부 변경 계획

### 백엔드 (Backend)
1. **`app/topics/discovery.py`**:
   - `_build_messages(focus_sns: bool = False)`의 프롬프트 지시어 변경:
     - `focus_sns=True`일 때, 뉴스 기사나 공식 발표는 완전히 제외하고 **오직(EXCLUSIVELY/ONLY) 온라인 커뮤니티, 포럼, 카페, SNS의 실제 이용자 게시글, 후기, 반응 및 핫이슈**에서만 주제를 수집하도록 강력한 제약사항 추가.

### 프론트엔드 (Frontend)
1. **`app/web/templates/index.html`**:
   - 체크박스 라벨을 `🔥 커뮤니티&SNS 전용`으로 변경.
   - 툴팁(title) 문구: "뉴스 기사를 제외하고 오직 온라인 커뮤니티, 카페, SNS 및 포럼 이슈/후기에서만 수집"
2. **`app/web/static/js/main.js`**:
   - 주제 수집 성공 시 완료 메시지 표현: `(커뮤니티&SNS 전용)`으로 업데이트.

### 테스트 및 검증 (Tests)
1. `tests/test_topic_discovery.py`: `focus_sns=True`일 때 "EXCLUSIVELY from online community" 지시어 포함 여부 검증 업데이트.
2. pytest 전체 테스트 실행 및 검증 완료.

## 3. 진행 현황
- [x] 변경 작업 계획 수립 및 마크다운 문서 업데이트
- [x] `app/topics/discovery.py` 커뮤니티 전용 프롬프트 반영
- [x] `index.html`, `main.js` UI 텍스트 업데이트 (`🔥 커뮤니티&SNS 전용`)
- [x] 테스트 코드 업데이트 및 전체 pytest 검증 완료
