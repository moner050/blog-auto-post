# 작업 계획서: .gitignore 프로젝트 구조 반영 및 storage 예외 처리

## 1. 작업 개요
프로젝트 구조(Python 가상환경, pytest 캐시, setuptools egg-info 빌드 산출물, 데이터베이스 등)에 맞게 `.gitignore`를 업데이트합니다.
단, `storage/` 디렉토리 내 사용자가 지정한 7개 썸네일 이미지 파일은 Git 추적 대상(무시 예외)으로 유지합니다.

## 2. 지정된 storage 예외 파일 목록 (Git 추적 유지)
- `storage/default_thumbnail.png`
- `storage/goverment-policy.png`
- `storage/goverment24.png`
- `storage/hometax.png`
- `storage/law.png`
- `storage/lifestyle.png`
- `storage/travel.png`

## 3. 세부 작업 항목
1. `.gitignore` 파일 수정
   - 환경 변수 (`.env`, `.env.*`)
   - Python 캐시 및 빌드 산출물 (`__pycache__/`, `*.egg-info/`, `build/`, `dist/`, `.pytest_cache/` 등)
   - DB 및 로그 파일 (`*.db`, `*.sqlite`, `*.log` 등)
   - 저장소(`storage/`) 처리: `storage/*` 전체 무시 설정 후 지정 7개 파일 개별 예외(`!storage/...`) 처리
   - IDE 및 OS 임시 파일
2. Git ignore 및 status 검증
   - `git check-ignore` 명령어로 예외 처리된 7개 파일이 무시되지 않는지 확인
   - `tistory_life_automation.egg-info/` 등 산출물이 정상 무시되는지 확인
