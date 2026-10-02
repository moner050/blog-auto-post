# 작업 계획서: 글쓰기 품질 파이프라인 및 Perplexity Agent API 전환

## 1. 개요
- Perplexity가 Sonar Chat Completions를 **2026-09-27에 종료**했다. 기존 요청은 Agent API로 "재구성"되어 당분간 동작하지만, 공식 매핑에서 `sonar`와 `sonar-pro`가 모두 `fast` 프리셋이 되고 `search_language_filter`는 지원되지 않는다. 그래서 호출 계층을 **Agent API(`POST /v1/agent`)** 로 옮겼다.
- 글 생성은 5줄짜리 범용 프롬프트였고, `configs/`의 페르소나·스타일 규칙(생성용 `llm_instructions` 포함)은 코드가 읽지 않았다. 규칙을 프롬프트와 검증 기준으로 연결하고, 결과를 정제·검증·재작성하는 파이프라인으로 바꿨다.
- 글 제목은 **주제 탐색의 어그로·고CTR 지향**을 따른다(`ARTICLE_TITLE_STYLE=clickbait`, 기본). 본문은 스타일 규칙(YAML)을 따른다.

## 2. 실호출로 확인한 사실 (2026-10-01)
### 2.1 토픽 탐색 요청 1회 (`fast` 프리셋)
- 보낸 필드가 모두 수락됨: `preset`, `instructions`, `input`(문자열), `max_output_tokens`, `temperature`, `store:false`, `language_preference:"ko"`, `tools:[{type:"web_search", filters:{search_recency_filter}, user_location:{country:"KR"}}]`, `response_format`(json_schema).
- 응답 JSON에는 **`output_text`가 없다**(SDK 편의 속성). 본문은 `output[]`의 `type:"message"` 항목 → `content[0].text`.
- `output[]`에는 `type:"search_results"` 항목이 먼저 오고(`queries`, `results[{id,url,title,snippet,date,last_updated,source}]`), `id`는 1부터 순차다. 모델이 쓴 `citation_indices`는 이 `id`와 의미상 일치했다.
- `fast` 프리셋은 `openai/gpt-6-luna`로 실행됐다(`model` 필드). `status:"completed"`, `usage.cost.total_cost = 0.00225` USD, 입력 6,109 / 출력 986 토큰.
- 응답 헤더 `x-ratelimit-limit: 3`(계정에 따라 낮은 한도), `x-ratelimit-reset`은 epoch 초.
- 이 응답으로 오프라인 검증: 새 파서가 `citations`를 `id` 순서로 정확히 정렬했고, 토픽 탐색 검증을 통과한 후보는 9개 중 9개였다. 응답 구조는 `tests/fixtures/perplexity_agent_response.json`(외부 기사 내용은 중립 값으로 치환)로 고정했다.

### 2.2 글 생성 요청 3회 (사용자가 승인한 상한)
| 실행 | 설정 | 결과 |
|---|---|---|
| A | `fast`, 주제 탐색이 만든 제목형 주제 + 출처 2건, 재작성 최대 1회 | 성공. 12.0초, 입력 10,709 / 출력 1,724 토큰, **$0.00284**, 검색 결과 10건. 태그 구조·`<article_sources>` 준수, 본문 내 `[n]` 표기·마크다운·링크 0건. 재작성 0회. 본문 2,125자로 권장 하한(2,200자)에 못 미쳐 경고 1건. 푸터 출처 3건(연합뉴스·YTN·금융위원회) |
| B | `low`, A와 같은 요청 | **HTTP 400(invalid request)**. 원인은 확인하지 못했다(`low`가 `temperature`를 받지 않는 것으로 추정할 뿐이다) |
| C | `fast`, 직접 입력한 how-to 주제, 재작성 0회 | 성공. 형식 준수, 비용·시간은 A와 비슷(건당 약 $0.0026~0.0028, 약 12초) |
- 글 한 편(검색 1회 포함)에 약 $0.003이라 비용은 문제가 되지 않았다.
- **제목이 약했다.** A에서 모델이 탐색이 만든 제목("DSR 규제한다더니 신규 대출 70%가 예외? …")을 쓰지 않고 정보 전달형("DSR 규제 예외 70.5%, 신규 대출 전 꼭 확인할 기준")으로 바꿨다. 그래서 (1) 탐색이 만든 제목형 주제는 그대로 쓰도록 지시(`title_seed`), (2) 직접 입력한 주제는 클릭 장치를 "반드시" 쓰도록 명령형으로 강화하고 페르소나의 순화 규칙보다 우선한다고 명시, (3) 질문·구체적 숫자·손해 회피나 변화를 말하는 표현이 하나도 없는 제목은 재작성 대상(`title.flat`)으로 바꿨다(`정부24`처럼 이름에 붙은 숫자는 숫자 장치로 세지 않는다). **이 세 가지는 실호출 없이 단위 테스트로만 확인했다.**
- `low` 400에 대한 대응: `low` 이상 프리셋에는 `temperature`를 보내지 않고, Agent API가 400을 돌려주면 `temperature`를 빼고 한 번 더 요청한다. **이 대응도 실호출로 확인하지 못했다.**

## 3. 문서만 보고 구현한 것 (실호출 미검증)
- `medium`/`high` 프리셋, `"provider/model"` 직접 지정. 응답 구조는 같다고 문서에 적혀 있으나 멀티스텝 실행에서 `results[].id` 번호 체계가 이어지는지는 확인하지 못했다. `low`는 위와 같이 400이었다.
- `language_preference`의 실제 효과(요청은 수락됨).
- 재작성 호출. 실호출 A·C는 재작성이 필요하지 않았다(같은 경로이므로 형식 문제는 낮지만 실사용 전 확인 필요).
- 제목 변경(2.2)의 효과. 첫 글 몇 편은 `generate-article --dry-run`과 로그(`article_llm_call`, `article_generated`)로 제목이 탐색 제목을 유지하는지 확인하라.

## 4. 변경 사항
### 4.1 `app/llm/client.py` — Perplexity 클라이언트
- 기본 Agent API(`PERPLEXITY_API_MODE=agent`), 레거시 `sonar` 모드는 임시 우회용으로 유지.
- Sonar 모델명 → 프리셋 자동 변환(공식 매핑), `search_language_filter` → `language_preference` + 한국어 자료 우선 지시.
- 429/5xx/연결 오류는 재시도(`Retry-After` 또는 `x-ratelimit-reset`을 따름), 읽기 타임아웃은 재시도하지 않음. 호출 1회의 총 대기 상한이 `timeout_seconds`.
- `finish_reason`(`status:"incomplete"` → `length`), `usage`, `model` 노출. 인용 번호 위치를 보존(비어 있는 번호는 빈 문자열).
- `low` 이상 프리셋에는 `temperature`를 보내지 않고, Agent API가 400/422를 돌려주면 `temperature`(레거시 모드는 `disable_search`)를 빼고 한 번 더 요청한다. 실호출에서 `low`가 400이었기 때문인데 원인은 확인하지 못했다.

### 4.2 글 생성 파이프라인 (`app/llm/`)
| 모듈 | 역할 |
|---|---|
| `style.py` | YAML을 읽어 모드(생활/기술)·구조 라우팅, 시스템 프롬프트와 검증 기준 생성. 파일이 없거나 깨지면 내장 기본 규칙(경고 포함). 어그로 제목 규칙은 생활 글에만 적용하고 기술 글은 설정과 상관없이 YAML의 차분한 제목 규칙을 따른다 |
| `prompts.py` | 출력 형식(태그 구조)·HTML 규칙, 요청 문맥(주제·카테고리·이유·사전 조사 자료). 외부 유래 텍스트는 태그 문자 제거·한 줄화. 탐색이 만든 제목형 주제(`title_seed`)는 제목을 그대로 쓰게 하고, 직접 입력한 주제는 클릭 장치를 넣어 새로 만들게 한다 |
| `parsing.py` | 태그 구조 해석(JSON 폴백), 제목·요약·태그 정리(길이 상한, 선형 시간), 잘림 감지, 근거 출처 번호(`<article_sources>`) 해석 |
| `sanitize.py` | 허용 태그·속성만 새로 직렬화, 출처 번호 제거, 표 스타일, 입력 크기(40만 자)·중첩(60단계) 상한, `<p>` 자동 닫기. 코드 블록 안의 꺾쇠 표기·HTML 예시와 HTML 요소가 아닌 이름(`List<String>`의 `<String>`)은 지우지 않고 글자로 남기며, 코드 밖의 그런 표기는 알린다 |
| `sources.py` | URL 검증(따옴표·꺾쇠·역슬래시·잘못된 포트 거부), 본문 링크 정책(검색으로 확인된 URL, 또는 공식 기관 대표 주소만. 공식은 `go.kr`·`gov.kr`과 공공기관 5곳이며 `or.kr` 전체는 믿지 않는다. 기본이 아닌 포트·쿼리가 붙은 변형은 거부), UGC 제외, 공식 기관 우선 정렬(`rank_sources`), 참고 자료 푸터(실제 호스트 표시, 비공식 링크에 `nofollow`) |
| `validation.py` | 분량·소제목·이모지·제한 표현·지어낸 경험담·마크다운 잔재·제목 숫자의 본문 근거·클릭 장치 없는 제목(`title.flat`) 등 결정적 검사 |
| `generator.py` | 작성 → 정제·검증 → (오류가 있으면) 재작성 최대 N회 → 푸터. 수정본이 더 나을 때만 채택하고, 수정 단계는 어떤 예외로 실패해도 이미 만든 초안을 유지한다. 수정본이 `<article_sources>`를 빠뜨리면 초안의 번호를 이어 쓰고, 근거를 밝히지 못했거나 공식·근거 출처가 아닌 외부 링크가 있으면 경고한다 |
| `textutil.py` | 모델·검색 결과 텍스트에서 보이지 않는 문자(제로폭, 방향 제어, 프롬프트에 글을 숨기는 태그 문자, 제어 문자)를 제거. 프롬프트 입력·모델 출력 필드·정제기 본문·출처에 쓴다 |
| `audit.py` | 저장된 글을 같은 기준으로 채점(생성 단계가 붙인 푸터는 빼고 센다) |

### 4.3 웹·CLI
- 토픽 후보의 **이유·출처·카테고리**를 생성기에 전달, 생성 단계마다 `updated_at`을 갱신(하트비트), stale 기준을 호출 대기 상한 + 45초로 연동, 응답에 `warnings` 포함.
- `show-prompt`(전송될 프롬프트 출력, API 호출 없음), `audit-articles`(저장된 글 채점).

### 4.4 설정 (`.env.example` 참고, 모두 선택)
`PERPLEXITY_API_MODE`, `PERPLEXITY_COUNTRY`, `ARTICLE_MODEL`, `ARTICLE_TEMPERATURE`, `ARTICLE_MAX_TOKENS`, `ARTICLE_MAX_REVISIONS`, `ARTICLE_TIMEOUT_SECONDS`, `ARTICLE_STYLE_RULES_PATH`, `ARTICLE_TITLE_STYLE`

## 5. 튜닝 방법
- 문체·구조·분량·제한 표현: `configs/tistory_blog_style_rules.yaml` 수정 후 `show-prompt`로 확인.
- 더 깊은 조사: `ARTICLE_MODEL=low` 이상. 호출당 비용·시간이 늘어난다. 단 `low`는 실호출에서 HTTP 400이었으므로(2.2) 먼저 `--dry-run`으로 확인한다.
- 비용 확인: 로그 `article_llm_call`의 `cost_usd`.
- 현재 글 수준 측정: `python -m app.cli audit-articles`(개선 전 기준선).
- 새 글의 품질 확인: `python -m app.cli generate-article --topic "..." --dry-run`(큐에 등록하지 않고 제목·태그·경고·출처·본문을 출력).

## 6. 알려진 한계
- 본문 사실의 정확성은 모델과 검색 결과에 의존한다. 규칙 검증은 형식·근거 연결(제목 숫자, 링크, 출처)을 잡을 뿐 사실 여부를 판정하지 않는다. 글은 비공개로 등록되므로 공개 전에 확인한다.
- 푸터는 모델이 `<article_sources>`로 밝힌 검색 결과(실호출에서 `results[].id`가 1부터 순차임을 확인)를 쓰고, 밝히지 않으면 이번 검색 결과 전체를 쓴다. 주제 탐색 때의 출처는 번호 매핑이 어긋났을 수 있어 푸터에 쓰지 않는다(본문 링크 허용 범위와 프롬프트 참고용으로만 쓴다).
- 정제기는 약 44만 건 무작위 퍼징과 3,072개 XSS 벡터에서 허용 문법 위반·활성 태그가 0건이었고 Chromium·Firefox·WebKit의 DOM으로도 확인했지만, Python 3.10의 `html.parser`(설치된 것은 3.14뿐이라 확인하지 못했다)와 Tistory 쪽 본문 재직렬화는 증명하지 못했다. 대시보드 미리보기는 `allow-*` 없는 sandbox iframe·CSP로 막혀 있다(코드 리뷰 W2).
- 본문 링크의 신뢰 기준은 "이번 검색 결과에 있었다"이다. 검색 결과 페이지가 프롬프트 주입을 담고 있으면 모델이 그 페이지로 링크를 걸 수 있다. 비공식 링크에 `nofollow`를 붙이고, 푸터에 실제 호스트를 함께 보여 주고, 글의 근거로 밝히지 않은 외부 호스트 링크는 경고로 알리지만 막지는 않는다(코드 리뷰 F3). 글은 비공개로 등록되므로 공개 전에 경고를 확인한다.
- 토픽 탐색 코드(`app/topics/discovery.py`)는 글 생성 파이프라인과 별개로 손봤다: 후보 안에서 쓸 수 있는 출처는 살리고, 잘린 JSON에서 완결된 후보를 건지고, 12개 상한을 검증 뒤에 적용하고, 버린 이유를 로그·오류 메시지에 남긴다(코드 리뷰 T1·T2·T5). 인용 번호가 범위 안에서 한 칸 어긋난 경우는 코드로 잡을 수 없고, 배치 간 중복 방지(T3)는 여전히 프롬프트 힌트에 의존한다.

## 7. 테스트
- `python -m pytest` 전체 통과. 테스트는 `conftest.py`가 셸의 환경 변수와 `.env`를 모두 지우고(운영 DB URL·API 키가 내보내져 있어도 영향받지 않는다는 것을 새 프로세스로 확인) 외부 네트워크 연결을 차단한다.
- 변이 점검: 처음에는 제가 고른 19곳만 확인했는데, 독립 리뷰어가 114곳을 시도하자 25개가 살아남았다(그중 7개는 실제 익스플로잇을 여는 가드). 부정 테스트를 보강하고 정제기 출력 검사를 금지어 검색에서 허용 문법 오라클(`tests/html_oracle.py`)로 바꾼 뒤, 리뷰어의 하니스에서 처음 살아남았던 11개가 모두 죽는 것까지 다시 확인했다. 이전 판의 "변이가 하나도 살아남지 않았다"는 19곳에 한정된 말이었다.
- 정제기 약 44만 건 퍼징(허용 문법 위반 0건)과 6,000개 출력의 실제 Chromium DOM 검사, 적대적 입력 처리 시간(30만 자에서도 1초 이하)을 별도로 확인했다.
- 글 생성 실호출은 3회(`fast` 2회 성공, `low` 1회 400). 제목 변경(`title_seed`, `title.flat`)은 실호출로 확인하지 못했다.

## 8. 진행 현황
- [x] Agent API 클라이언트 + 실호출 검증 + 응답 픽스처
- [x] 스타일 규칙 → 프롬프트/검증 연결, 제목 방향(어그로) 적용
- [x] 정제·링크 정책·검증·재작성·푸터, 독립 리뷰 15건 처리
- [x] 웹·CLI 연동, 문서
- [x] 글 생성 실호출 검증(`fast`: 분량·형식 준수·비용·지연 확인)
- [ ] `low` 이상 프리셋 실호출 확인(400 원인)
- [ ] 제목 변경 후 실호출 1~2회로 효과 확인
