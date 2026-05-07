@echo off
cd /d %~dp0

echo [*] Starting OutboundAI Mass Caller...

REM Load .env variables (basic support)
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v "^#" .env`) do (
        set %%A=%%B
    )
)

echo [+] Configuration:
echo    LiveKit: %LIVEKIT_URL%
echo    LLM:     %LLM_PROVIDER%
echo    DB:      %DB_HOST%:%DB_PORT%/%DB_NAME%

echo [*] Starting FastAPI server on port 8000...

REM Start server in background
start "" cmd /c uvicorn server:app --host 0.0.0.0 --port 8000

REM Wait for server to start
timeout /t 3 >nul

echo [*] Starting LiveKit agent worker...
python agent.py start

echo [!] Stopping server...
taskkill /f /im uvicorn.exe >nul 2>&1

echo [OK] Done.
pause
