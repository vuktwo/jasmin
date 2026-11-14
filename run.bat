@echo off
setlocal
title Jasmin Launcher

:: Always use the folder containing this batch file.
cd /d "%~dp0"

echo [1/4] Starting Cloudflare Tunnel...

:: Remove an old log so we cannot accidentally reuse an old tunnel URL.
if exist "%~dp0cloudflared.log" del "%~dp0cloudflared.log"

:: Start cloudflared in its own window.
start "Cloudflare Tunnel" /D "%~dp0" cmd /k "cloudflared tunnel --url http://127.0.0.1:8000 > cloudflared.log 2>&1"

echo [2/4] Waiting for the new tunnel URL...
:: The helper waits up to 60 seconds and prints only the URL on success.
for /f "usebackq delims=" %%i in (`python "%~dp0get_tunnel_url.py"`) do set "TUNNEL_URL=%%i"

if not defined TUNNEL_URL (
    echo ERROR: Could not find a tunnel URL after waiting.
    echo Check cloudflared.log and make sure cloudflared is installed and on PATH.
    echo ERROR: Tunnel URL was empty.
    pause
    exit /b 1
)

echo Tunnel URL: %TUNNEL_URL%

echo [3/4] Saving link.txt and updating .env...
>"%~dp0link.txt" echo(%TUNNEL_URL%
python "%~dp0update_env.py" "%TUNNEL_URL%"
if errorlevel 1 (
    echo ERROR: Failed to update .env.
    pause
    exit /b 1
)

echo [4/4] Starting web server and Discord bot...

start "Jasmin Web Server" /D "%~dp0" cmd /k "python -m uvicorn webapp:app --host 127.0.0.1 --port 8000"
start "Jasmin Bot" /D "%~dp0" cmd /k "python main.py"

echo.
echo Startup commands launched.
echo Tunnel URL saved in link.txt.
echo Redirect URI updated in .env.
timeout /t 3 >nul
endlocal
