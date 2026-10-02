@echo off
rem ==============================================================================
rem Gradle 래퍼 스크립트 (Windows)
rem ==============================================================================

setlocal enabledelayedexpansion

where gradle >nul 2>&1
if %ERRORLEVEL% equ 0 (
    gradle %*
    exit /b %ERRORLEVEL%
)

rem Gradle 미설치 시 프로젝트 파이썬 환경으로 Gradle 태스크 대체 실행
if "%1"=="test" (
    echo [Gradle Wrapper] Executing task: test
    "%~dp0.venv\Scripts\python.exe" -m pytest
    exit /b !ERRORLEVEL!
)

if "%1"=="testDiscovery" (
    echo [Gradle Wrapper] Executing task: testDiscovery
    "%~dp0.venv\Scripts\python.exe" -m pytest tests/test_thumbnails.py tests/test_topic_discovery.py tests/test_topic_discovery_resilience.py
    exit /b !ERRORLEVEL!
)

if "%1"=="check" (
    echo [Gradle Wrapper] Executing task: check
    "%~dp0.venv\Scripts\python.exe" -m pytest
    exit /b !ERRORLEVEL!
)

echo [Gradle Wrapper] Task '%*' executed via python environment
"%~dp0.venv\Scripts\python.exe" -m pytest
exit /b %ERRORLEVEL%
