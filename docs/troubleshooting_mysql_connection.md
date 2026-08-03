# MySQL Connection Reset (Error 2006) 원인 분석 및 해결 계획

## 1. 개요
AI 주제 수집 API (`POST /api/topic-candidates/discover`) 호출 중 발생한 `500 Internal Server Error` 및 `pymysql.err.OperationalError: (2006, "MySQL server has gone away")` 오류의 원인을 분석하고 해결 방안을 정의합니다.

## 2. 발생 원인
1. **장시간 유휴 상태 (Idle Connection)**:
   - FastAPI (uvicorn) 애플리케이션 프로세스가 수시간 이상 오랫동안 실행되면서, DB 커넥션 풀에 유지되고 있던 기존 연결이 오랫동안 사용되지 않음.
2. **MySQL 서버 타임아웃**:
   - MySQL 서버의 `wait_timeout` 설정이나 네트워크/방화벽 타임아웃에 의해 원격 호스트(MySQL)가 유휴 소켓 연결을 강제로 끊음 (`ConnectionResetError: [WinError 10054]`).
3. **Stale Connection 재사용**:
   - `discover_topic_candidates` 요청 처리 중 DB 세션이 이미 끊어진 커넥션을 풀에서 그대로 받아와 `session.commit()`을 수행할 때 쿼리 송신 실패가 발생함.

## 3. 해결 작업 계획
1. `app/db/session.py`의 `create_engine` 옵션 보완:
   - `pool_pre_ping=True`: 커넥션을 사용하기 직전에 살아있는지 유효성을 검사하고 끊어진 경우 자동 재연결.
   - `pool_recycle=3600`: 1시간(3600초) 이상 지난 커넥션을 알아서 폐기하고 재연결하도록 설정.
2. 수정 완료 후 서버 및 DB 연결 검증.

## 4. 진행 현황
- [x] 원인 분석 완료
- [x] `app/db/session.py` 수정 (`pool_pre_ping=True`, `pool_recycle=3600`)
- [x] unit test 검증 완료
