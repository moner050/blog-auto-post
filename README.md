# 🚀 티스토리 생활꿀팁 글 자동화 시스템 & 관리자 웹 대시보드

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Dashboard-green.svg)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/playwright-automation-green.svg)](https://playwright.dev/)
[![Perplexity API](https://img.shields.io/badge/Perplexity-Sonar_LLM-purple.svg)](https://docs.perplexity.ai/)

**Tistory Auto Post**는 **Perplexity Sonar LLM API**의 실시간 웹 탐색(Web-grounded) 기능으로 최신 포스팅을 자동 생성하고, **FastAPI 기반 관리자 웹 대시보드** 및 **Playwright 브라우저 자동화**를 통해 티스토리에 안전하게 비공개 포스팅 및 검증을 수행하는 종합 자동화 시스템입니다.

---

## 🖥️ 관리자 웹 대시보드 (Admin Web Dashboard)

CLI 명령어 외에도 브라우저 화면에서 모든 포스팅 상태 및 자동화 동작을 시각적으로 제어할 수 있습니다.

```bash
# 관리자 웹 대시보드 서버 실행 (http://localhost:9000)
python -m app.cli server

# 포트 번호를 임의로 변경하여 실행하는 경우
python -m app.cli server --port 9000
```

### 🌟 웹 대시보드 주요 기능
1. **📊 포스팅 & 작업 큐 현황 모니터링**: 전체 포스팅 수, 발행 완료, 대기 중, 실패/격리 건수를 실시간 카드로 조망.
2. **🤖 웹 화면에서 즉시 AI 글 생성**: 주제(Topic)와 카테고리를 입력하고 버튼을 누르면 Perplexity Sonar API로 포스팅을 자동 생성 후 DB 큐에 즉시 등록.
3. **⚡ 발행 워커 1회 즉시 실행**: 버튼 클릭 한 번으로 Playwright 포스팅 발행 워커를 즉시 동작시킴.
4. **👁️ 포스팅 HTML 본문 미리보기**: 생성된 글의 제목, 태그, HTML 본문을 모달(Modal) 창에서 직접 미리보기.

---

## 🌟 주요 백엔드 기능

1. **🤖 Perplexity Sonar API 기반 포스팅 자동 생성**
   - 실시간 웹 정보 탐색으로 최신 정부24, 홈택스, 공공기관 가이드 반영.
   - 프롬프트 캐싱(Prompt Caching) 최적화로 빠른 생성 및 API 비용 절감.

2. **🗄️ MySQL / SQLite 지원 작업 큐 (Job Queue)**
   - SQLAlchemy ORM 및 Alembic 마이그레이션 적용.

3. **🎭 Playwright 기반 티스토리 자동 비공개 발행 & 비로그인 보안 검증**
   - 세션 쿠키 저장으로 지속 로그인 유지. 발행 직후 익명 브라우저로 비공개 노출 여부 검증.

4. **🛠️ UI 선택자 분리 관리 (`configs/tistory_selectors.yaml`)**
   - 티스토리 에디터 UI 디자인 변경 시 소스코드 수정 없이 YAML 수정만으로 대응.

---

## ⚙️ 환경 설정 (`.env`)

```env
# 1. MySQL 데이터베이스 접속 정보
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_db_user
MYSQL_PASSWORD=your_db_password
MYSQL_DATABASE=blog_auto_post

# 2. 티스토리 자동화 설정
TISTORY_EXPECTED_BLOG_NAME=본인의_블로그명  # 예: lmh 의 일상
TISTORY_ALLOWED_CATEGORY=포스팅할_카테고리명 # 예: 홈텍스, 생활행정
AUTO_PUBLISH_ENABLED=true
TISTORY_PRODUCTION_ENABLED=true

# 3. Perplexity LLM 설정
PERPLEXITY_API_KEY=pplx-your_api_key_here
PERPLEXITY_MODEL=sonar
PERPLEXITY_BASE_URL=https://api.perplexity.ai
```

---

## 📖 실행 방법 가이드 (CLI & Web)

### 1단계: DB 테이블 초기화
```bash
python -m app.cli init-db
```

### 2단계: 티스토리 브라우저 최초 로그인 (1회 필수)
```bash
python -m app.cli login
```

### 3단계: 관리자 웹 대시보드 서버 실행 (포트 9000)
```bash
python -m app.cli server
# 브라우저에서 http://localhost:9000 접속
```

### (대안) CLI 직접 사용 방식
- **AI 글 생성 & 큐 등록**: `python -m app.cli generate-article --topic "주민등록등본 발급방법"`
- **발행 워커 실행**: `python -m app.cli worker-once`

---

## 🧪 단위 테스트 실행 (Testing)

```bash
python -m pytest
```

---

## 🚨 트러블슈팅: IDE 모듈 임포트 및 컴파일 에러 해결 방법

VS Code나 PyCharm 등 IDE에서 `pydantic_settings`, `playwright`, `alembic`, `uvicorn` 구문에 밑줄(Red Squiggles)이 표시되며 임포트/컴파일 에러가 발생할 때 해결 방법입니다.

### 📌 에러 발생 원인
1. **가상환경 미생성 또는 미활성화**: 필수 패키지들이 설치된 독립된 Python 가상환경이 없거나 활성화되지 않음.
2. **의존성 패키지 미설치**: `pyproject.toml`에 등록된 패키지가 현재 Python 환경에 로드되지 않음.
3. **IDE Python Interpreter 미선택**: VS Code 등의 에디터 언어 서버(Pylance / Pyright)가 프로젝트 `.venv` 가상환경 경로를 바라보고 있지 않음.

### 🛠️ 해결 방법

#### 1단계: 파이썬 가상환경 생성 및 활성화
```bash
# 프로젝트 루트 경로에서 가상환경 생성
python -m venv .venv

# 가상환경 활성화 (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# (Linux / macOS 사용자)
source .venv/bin/activate
```

#### 2단계: 의존성 패키지 및 Playwright 설치
```bash
# 모든 의존성 라이브러리 설치 (MySQL, Test 옵션 포함)
pip install -e .[mysql,test]

# Playwright 자동화용 브라우저 설치 (최초 1회)
playwright install
```

#### 3단계: IDE Python 인터프리터 경로 지정 (VS Code 기준)
1. `Ctrl + Shift + P` (macOS: `Cmd + Shift + P`) 단축키 입력
2. **`Python: Select Interpreter`** 항목 선택
3. 목록에서 **`.\.venv\Scripts\python.exe`** (Enter interpreter path -> `.\.venv\Scripts\python.exe` 입력 가능) 지정

---

## 📁 프로젝트 주요 구조

```text
blog-auto-post/
├── app/
│   ├── cli.py                  # CLI 커맨드 (init-db, login, generate-article, server, worker-once)
│   ├── web/                    # FastAPI 기반 관리자 웹 대시보드 (app.py, templates, static/css, static/js)
│   ├── content/                # 포스팅 데이터 등록 모듈
│   ├── core/                   # 환경 설정 (settings.py) 및 로깅 모듈
│   ├── db/                     # SQLAlchemy 모델 및 DB 세션 관리
│   ├── jobs/                   # 작업 큐 (queue.py) 및 발행 워커 (worker.py)
│   ├── llm/                    # Perplexity Sonar API 연동 모듈 (client, prompts, generator)
│   └── publishing/             # Playwright 티스토리 자동화 발행 엔진 (tistory.py)
├── configs/
│   ├── tistory_selectors.yaml  # 티스토리 에디터 UI 선택자 설정
│   └── tistory_blog_persona.md # 블로그 페르소나 가이드
├── docs/                       # 아키텍처 및 연동 설계 문서
├── tests/                      # Pytest 단위 및 통합 테스트 (22개 테스트 통과)
├── .env.example                # 환경 변수 예시 파일
└── pyproject.toml              # 프로젝트 의존성 설정 파일
```
