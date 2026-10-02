# 🚀 티스토리 생활꿀팁 글 자동화 시스템 & 관리자 웹 대시보드

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Dashboard-green.svg)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/playwright-automation-green.svg)](https://playwright.dev/)
[![Perplexity API](https://img.shields.io/badge/Perplexity-Agent_API-purple.svg)](https://docs.perplexity.ai/docs/agent-api/quickstart)

**Tistory Auto Post**는 **Perplexity Agent API**의 웹 검색 기반 생성으로 글 주제를 추천받고 포스팅을 자동 작성한 뒤, **Playwright 브라우저 자동화**로 티스토리에 **비공개로만** 발행하고 비로그인 상태에서 정말 비공개인지 검증하는 자동화 시스템입니다. **FastAPI 관리자 웹 대시보드**에서 전 과정을 다룰 수 있습니다.

> ⚠️ Perplexity의 Sonar Chat Completions는 2026-09-27에 종료되었습니다. 이 프로젝트는 기본으로 Agent API(`POST /v1/agent`)를 사용하며, 기존 `PERPLEXITY_MODEL=sonar` 설정은 공식 매핑에 따라 `fast` 프리셋으로 동작합니다. (자세한 내용은 아래 [글 생성 파이프라인](#-글-생성-파이프라인) 참고)

> 🧪 **처음 발행하기 전에 꼭 읽어 주세요.** 발행기는 실제 Tistory 에디터로 아직 검증되지 않았습니다(가정 A1~A12, `app/publishing/tistory.py` 머리말). 운영에 쓰기 전에 **비공개 글 1건으로 수동 드라이런**을 하고 아래 세 가지를 확인하세요. 하나라도 다르면 발행은 공개로 새지 않고 매번 멈추며, 그때는 코드의 상수를 실제 화면에 맞춰야 합니다.
> 1. 발행 레이어를 여는 버튼 문구가 정확히 **'완료'** 인가
> 2. 비공개를 고른 뒤 최종 버튼 문구에 **'비공개'** 가 들어가는가(예: '비공개 저장', `REQUIRED_FINAL_LABEL`)
> 3. 로그인하지 않은 브라우저로 **블로그 홈(`/`)이 정상(HTTP 200)으로** 열리는가

---

## 🖥️ 관리자 웹 대시보드 (Admin Web Dashboard)

CLI 명령어 외에도 브라우저 화면에서 모든 포스팅 상태 및 자동화 동작을 제어할 수 있습니다.

```bash
# 관리자 웹 대시보드 서버 실행 (http://127.0.0.1:9000)
python -m app.cli server

# 포트 변경 / 코드 변경 시 자동 재로딩
python -m app.cli server --port 9100 --reload
```

### 🌟 웹 대시보드 주요 기능
1. **📊 포스팅 & 작업 큐 현황**: 전체 포스팅 수, 발행 완료, 대기 중, 실패/격리 건수를 카드로 보여 줍니다.
2. **💡 AI 주제 추천**: Perplexity 웹 검색으로 최근 이슈 기반 주제 후보를 받아 목록으로 보여 줍니다. '커뮤니티&SNS 위주', '참신·이색' 옵션이 있고, 후보를 누르면 추천 이유와 출처를 볼 수 있습니다. 후보에서 바로 글을 생성할 수 있고, 실패했거나 생성이 멈춘 후보('생성 멈춤')는 다시 생성하거나 삭제할 수 있습니다.
3. **🤖 주제를 직접 입력해 AI 글 생성**: 주제와 카테고리를 입력하면 글을 생성해 비공개 발행 큐에 등록합니다.
4. **⚡ 발행 워커 1회 즉시 실행**: 대기 중인 발행 작업 1건을 처리합니다. 결과 문구는 실제 처리 결과(성공·실패·비활성·대기 없음)를 그대로 보여 줍니다.
5. **👁️ 포스팅 미리보기·재등록·삭제**: 제목, 태그, HTML 본문을 미리 보고, 실패한 글을 다시 등록하거나 삭제합니다.

### 🔒 대시보드 보호
이 서버는 유료 LLM 호출, 글 삭제, 로그인된 브라우저 발행을 실행하므로 기본적으로 **내 PC에서만** 쓰도록 막혀 있습니다.

- **Host 검사**: `DASHBOARD_ALLOWED_HOSTS`(기본 `127.0.0.1,localhost,::1`)에 없는 이름으로 접속하면 400입니다. `0.0.0.0`·PC 이름·LAN IP로 접속하려면 그 이름을 추가하세요.
- **다른 사이트가 보낸 요청 차단**: 다른 웹 페이지에서 보낸 상태 변경 요청(POST·DELETE)은 403입니다. 리버스 프록시를 쓴다면 원래 Host 헤더를 유지하세요.
- **인증**: `DASHBOARD_PASSWORD`를 설정하면 모든 경로에 HTTP Basic 인증(`DASHBOARD_USER`, 기본 `admin`)을 요구합니다. 루프백이 아닌 `--host`로 열려면 비밀번호가 반드시 있어야 하고, 없으면 서버가 시작되지 않습니다.
- **미리보기**: 모델이 만든 HTML 본문은 스크립트·same-origin이 모두 막힌 sandbox iframe 안에서만 렌더링됩니다.
- **재등록**: 이미 발행됐거나 실행 중이거나 검증되지 않은 글, 티스토리 글 주소가 기록된 글을 다시 등록하려면 서버가 알려 주는 사유를 확인하고 한 번 더 승인해야 합니다. 승인해서 다시 등록해도 이전에 만들어진 글의 주소는 `verification_details.previous_post_urls`에 이력으로 남습니다(공개일 수 있으니 티스토리 글 관리에서 정리하세요).
- **삭제**: 발행 작업이 실행 중인 글은 삭제할 수 없습니다.
- CSP 때문에 `/docs`·`/redoc` 화면은 비어 보입니다(`/openapi.json`은 동작합니다).

---

## 🌟 주요 백엔드 기능

1. **🤖 Perplexity Agent API 기반 포스팅 자동 생성**
   - 웹 검색으로 정부24, 홈택스, 공공기관 안내 등 최신 정보를 반영합니다.
   - `configs/tistory_blog_style_rules.yaml`(페르소나·구조·분량·제한 표현)을 프롬프트로 읽고, 결과를 검증·정제·재작성한 뒤 출처 목록을 붙여 큐에 등록합니다.
   - 호출 실패(429/5xx)는 `Retry-After`/`x-ratelimit-reset`을 따라 자동 재시도합니다.

2. **💡 주제 탐색 (`app/topics/discovery.py`)**
   - 응답의 후보 중 일부가 형식에 맞지 않거나 출처 번호가 틀려도, 쓸 수 있는 후보만 골라 저장합니다. 응답이 중간에 잘려도 완결된 후보는 살립니다.
   - 버려진 후보와 그 이유는 경고 로그(`kept N candidates; dropped: ...`)에 남습니다.

3. **🗄️ MySQL / SQLite 작업 큐 (Job Queue)**
   - SQLAlchemy ORM 및 Alembic 마이그레이션을 씁니다.
   - 브라우저 단계에 들어가기 전에 상태를 커밋해 두므로 워커가 죽어도 흔적이 남습니다. `PUBLISH_LOCK_TTL_SECONDS`(기본 1800초, 최소 600초)를 넘겨 멈춘 작업은 **자동으로 다시 실행하지 않고** 사람이 확인할 상태로 돌립니다(발행 단계 전에 멈춘 작업은 글이 없으므로 격리하지 않습니다).
   - 이미 글이 있을 수 있는 발행 건(글 주소가 기록됐거나 발행 중·확인 불가·발행 확인 상태)은 워커가 다시 발행하지 않습니다(`DUPLICATE_GUARD`).

4. **🎭 Playwright 기반 티스토리 비공개 발행 & 비로그인 검증**
   - 저장된 브라우저 프로필로 로그인을 유지합니다.
   - 비공개 선택이 증명되기 전에는 최종 발행 버튼을 누르지 않습니다. 최종 버튼은 문구에 '비공개'가 있을 때만 누르고, 클릭 직전과 클릭 이벤트 시점에 선택 상태를 다시 확인합니다.
   - '공개로 발행됩니다' 같은 확인창은 취소하고 발행을 멈춥니다. 모든 확인창은 로그(`tistory_dialog`)에 남습니다.
   - 발행 직후 비로그인 브라우저로 글을 열어 정말 읽을 수 없는지 확인합니다. 같은 브라우저로 블로그 홈이 열리는지(대조군)를 먼저 보고, 간격을 두고 두 번 확인합니다. 판단 근거는 `verification_details.evidence`에 남습니다.
   - 같은 프로필을 쓰는 브라우저 창이 열려 있어 브라우저를 못 띄우면 `BROWSER_LAUNCH_FAILED`로 기록되고 글은 격리되지 않습니다. 발행 중에는 로그인용 브라우저 창(`login`)을 닫아 두세요.

5. **🛠️ UI 선택자 설정 (`configs/tistory_selectors.yaml`)**
   - 블로그 이름·제목 입력란·비공개 문구 등 일부 선택자를 YAML에서 읽습니다. 다만 발행 흐름의 핵심 선택자(레이어 열기, 비공개 옵션, 최종 버튼 등)는 `app/publishing/tistory.py`에 상수로 있어, 티스토리 UI가 바뀌면 코드도 함께 고쳐야 합니다.

### 📋 발행 결과 상태 읽는 법
| 상태 / 오류 코드 | 뜻 | 할 일 |
|---|---|---|
| `VERIFIED` | 비공개로 발행됐고 비로그인 검증까지 통과 | 없음 |
| `UI_BROKEN` | 최종 발행 버튼을 누르기 **전에** 멈춤(글 없음) | 화면이 바뀌었는지 확인 후 재등록 |
| `PUBLISH_UNVERIFIED` | 버튼을 누른 **뒤** 확인하지 못함(글이 있을 수 있고 공개일 수도 있음) | 티스토리 글 관리에서 같은 제목의 글과 공개 상태를 확인. 다시 올리려면 강제 재등록 |
| `AUTH_REQUIRED` | 로그인 세션이 없음 | `python -m app.cli login` |
| `BLOCKED` / `PREFLIGHT_FAILED` | 발행 전 검사에서 막힘(비공개가 아님, 썸네일 없음 등) | 사유 확인 |
| `BROWSER_LAUNCH_FAILED` | 브라우저를 띄우지 못함(글 없음) | 같은 프로필의 다른 창을 닫고 재등록 |
| `STALE_RUNNING` | 실행 중 상태로 너무 오래 멈춘 작업 | 메시지에 '브라우저를 열기 전'이 있으면 재등록, 아니면 티스토리 확인 |
| `DUPLICATE_GUARD` | 이미 글이 있을 수 있어 다시 발행하지 않음 | 티스토리 확인 후 필요하면 강제 재등록 |

`QUARANTINED`(격리)된 글은 사람이 확인할 때까지 자동으로 다시 처리되지 않습니다.

---

## ⚙️ 환경 설정 (`.env`)

[.env.example](.env.example)을 복사해 `.env`를 만드세요. 주요 항목은 아래와 같습니다.

```env
# 1. 데이터베이스: DATABASE_URL을 직접 주거나(우선), MYSQL_* 값으로 자동 조립
# DATABASE_URL=sqlite:///storage/app.db
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_db_user
MYSQL_PASSWORD=your_db_password
MYSQL_DATABASE=blog_auto_post

# 2. 티스토리 자동화 설정
TISTORY_EXPECTED_BLOG_NAME=본인의_블로그명   # 예: lmh 의 일상
TISTORY_ALLOWED_CATEGORY=포스팅할_카테고리명 # 예: 홈텍스
TISTORY_WRITE_URL=https://본인블로그.tistory.com/manage/newpost
AUTO_PUBLISH_ENABLED=false        # 드라이런 전에는 false 권장(둘 다 true여야 워커가 발행)
TISTORY_PRODUCTION_ENABLED=false

# 3. Perplexity LLM 설정
PERPLEXITY_API_KEY=pplx-your_api_key_here
PERPLEXITY_MODEL=sonar            # Sonar 이름은 Agent API 프리셋으로 자동 변환 (sonar -> fast)
PERPLEXITY_BASE_URL=https://api.perplexity.ai

# 4. 대시보드 보호·발행 잠금 (선택)
# DASHBOARD_ALLOWED_HOSTS=127.0.0.1,localhost,::1
# DASHBOARD_PASSWORD=
# PUBLISH_LOCK_TTL_SECONDS=1800   # 600 이상
```

- 코드 기본값은 `AUTO_PUBLISH_ENABLED=false`·`TISTORY_PRODUCTION_ENABLED=false`(발행 꺼짐)입니다. `.env.example`은 둘 다 `true`로 시작하니, 복사한 뒤 드라이런 전까지는 `false`로 바꾸는 것을 권합니다. 꺼져 있으면 워커는 작업을 건드리지 않고 `DISABLED`로 끝나며, 다시 켜면 큐가 그대로 이어집니다.
- 글 생성 관련 선택 설정(`ARTICLE_MODEL`, `ARTICLE_MAX_REVISIONS`, `ARTICLE_TITLE_STYLE`, `PERPLEXITY_API_MODE`, `PERPLEXITY_COUNTRY` 등)은 [.env.example](.env.example)에 설명이 있습니다.

---

## 📖 실행 방법 가이드 (CLI & Web)

### 1단계: DB 테이블 초기화 (마이그레이션 적용)
```bash
python -m app.cli init-db
```

### 2단계: 티스토리 브라우저 최초 로그인 (1회 필수)
```bash
python -m app.cli login
# 열린 브라우저에서 로그인한 뒤 터미널에서 Enter
```

### 3단계: 관리자 웹 대시보드 서버 실행 (포트 9000)
```bash
python -m app.cli server
# 브라우저에서 http://127.0.0.1:9000 접속
```

### (대안) CLI 직접 사용 방식
| 명령 | 설명 |
|---|---|
| `python -m app.cli generate-article --topic "주민등록등본 발급방법" [--category 카테고리] [--thumbnail 경로]` | AI 글 생성 후 비공개 발행 큐에 등록 |
| `python -m app.cli generate-article --topic "..." --dry-run` | 글만 생성해 출력(큐 등록 없음, Perplexity 호출 1~2회 비용 발생) |
| `python -m app.cli show-prompt --topic "..." --category "정부지원·민원"` | 전송될 프롬프트 미리보기(API 호출 없음) |
| `python -m app.cli enqueue-static --title "제목" --html-file 본문.html --thumbnail 이미지.png --tags "태그1,태그2" [--category 카테고리]` | 직접 쓴 HTML 글을 큐에 등록 |
| `python -m app.cli worker-once` | 발행 워커 1회 실행 |
| `python -m app.cli audit-articles` | 저장된 글을 현재 스타일 규칙으로 채점 |
| `python -m app.cli server [--host 127.0.0.1] [--port 9000] [--reload]` | 관리자 웹 대시보드 실행 |

---

## ✍️ 글 생성 파이프라인

```text
주제 후보(주제·이유·출처·카테고리)
  → 프롬프트 조립: configs/tistory_blog_style_rules.yaml(페르소나·구조·분량·제한 표현) + 요청 문맥
  → Perplexity Agent API (웹 검색 기반 초안)
  → 해석(태그 구조, JSON 폴백) → HTML 정제(허용 태그만, 출처 번호 제거, 표 스타일)
  → 링크 검증(검색으로 확인된 URL·공식 기관 대표 주소만 유지)
  → 규칙 검증(분량·소제목·제한 표현·지어낸 경험담·제목 숫자의 본문 근거 등)
  → 오류가 있으면 해당 항목만 고치는 재작성 1회 (개선될 때만 채택)
  → '참고한 자료' 목록 + 확인일 안내를 글 끝에 추가 → 비공개 발행 큐
```

- **문체·구조를 바꾸려면** `configs/tistory_blog_style_rules.yaml`만 수정하세요. 코드가 그 파일을 읽어 프롬프트와 검증 기준으로 씁니다. 반영 결과는 `show-prompt`로 확인할 수 있습니다.
- **제목**은 기본으로 주제 탐색과 같은 어그로·고CTR 방향입니다(`ARTICLE_TITLE_STYLE=clickbait`). 제목이 약속한 숫자는 본문에 근거가 있어야 하며, 없으면 재작성 대상이 됩니다. 차분한 제목 규칙을 쓰려면 `persona`로 바꾸세요. 이 제목 규칙은 아직 실호출로 확인하지 못했습니다.
- **모델**은 기본 `fast` 프리셋이며 실호출로 확인했습니다(글 한 편에 약 12초, 약 $0.003). 더 깊은 조사를 원하면 `ARTICLE_MODEL=low` 이상을 시도할 수 있지만, 2026-10-01 실호출에서 `low`는 HTTP 400을 돌려줘 **아직 검증되지 않았습니다**(원인 미확인). 쓰기 전에 `generate-article --dry-run`으로 한 번 확인하세요. 실제 응답에는 `usage.cost`가 포함되며 로그(`article_llm_call`)에 기록됩니다.
- **경고**(분량 부족, 링크 제거, 근거로 밝히지 않은 외부 링크 등)는 글 생성 API 응답의 `warnings`와 CLI 로그에 남습니다. 글은 비공개로 등록되므로 공개 전에 확인하세요.
- Agent API는 계정에 따라 요청 한도가 낮을 수 있습니다(관측: `x-ratelimit-limit: 3`). 동시에 여러 글을 생성하지 마세요.
- 설계 배경, 실호출로 검증한 사실과 미검증 항목은 [docs/feature_article_quality_pipeline.md](docs/feature_article_quality_pipeline.md)에 정리되어 있습니다.

---

## 🧪 테스트 실행 (Testing)

```bash
python -m pytest
```

- 테스트는 실제 `.env`와 셸 환경 변수를 읽지 않고(메모리 SQLite, 빈 API 키), 외부 네트워크 연결을 차단합니다. 실수로 유료 API를 부르거나 운영 DB를 건드리지 않습니다.
- 발행기 테스트는 가짜 Page로 실행되며 실제 브라우저나 Tistory에 접속하지 않습니다. 실제 Tistory와의 일치는 위의 수동 드라이런으로 확인해야 합니다.
- Python 3.14에서 전체 테스트가 통과하는 것을 확인했습니다. `pyproject.toml`은 3.10 이상을 허용하지만 3.10에서는 실행해 보지 않았습니다.

---

## 🚨 트러블슈팅: IDE 모듈 임포트 및 컴파일 에러 해결 방법

VS Code나 PyCharm 등 IDE에서 `pydantic_settings`, `playwright`, `alembic`, `uvicorn` 구문에 밑줄(Red Squiggles)이 표시되며 임포트/컴파일 에러가 발생할 때 해결 방법입니다.

### 📌 에러 발생 원인
1. **가상환경 미생성 또는 미활성화**: 필수 패키지들이 설치된 독립된 Python 가상환경이 없거나 활성화되지 않음.
2. **의존성 패키지 미설치**: `pyproject.toml`에 등록된 패키지가 현재 Python 환경에 로드되지 않음.
3. **IDE Python Interpreter 미선택**: VS Code 등의 에디터 언어 서버(Pylance / Pyright)가 프로젝트 `.venv` 가상환경 경로를 바라보고 있지 않음.
4. **가상환경을 만든 Python이 삭제됨**: `.venv`를 만든 Python 버전을 제거하면 `.venv`가 동작하지 않습니다. `.venv`를 지우고 다시 만드세요.

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
│   ├── cli.py                  # CLI 커맨드 (init-db, login, enqueue-static, generate-article, show-prompt, audit-articles, server, worker-once)
│   ├── web/                    # FastAPI 관리자 웹 대시보드 (app.py, templates, static/css, static/js)
│   ├── content/                # 포스팅·썸네일 등록 모듈
│   ├── core/                   # 환경 설정 (settings.py) 및 로깅 모듈
│   ├── db/                     # SQLAlchemy 모델 및 DB 세션 관리
│   ├── jobs/                   # 작업 큐 (queue.py) 및 발행 워커 (worker.py)
│   ├── llm/                    # Perplexity 연동 및 글 생성 (client, style, prompts, parsing, sanitize, sources, validation, generator, audit)
│   ├── topics/                 # AI 주제 추천 (discovery.py)
│   └── publishing/             # Playwright 티스토리 발행 엔진 (tistory.py) 및 발행 전 검사 (guards.py)
├── configs/
│   ├── tistory_blog_style_rules.yaml # 글 문체·구조·분량·제한 표현 규칙 (프롬프트와 검증 기준)
│   ├── tistory_blog_persona.md       # 블로그 페르소나 가이드
│   └── tistory_selectors.yaml        # 티스토리 에디터 UI 선택자 설정 (일부)
├── migrations/                 # Alembic 마이그레이션
├── docs/                       # 설계·기능·리뷰 문서 (코드 리뷰: docs/code_review_2026-10-01.md)
├── tests/                      # Pytest 테스트 (python -m pytest)
├── .env.example                # 환경 변수 예시 파일
└── pyproject.toml              # 프로젝트 의존성 설정 파일
```
