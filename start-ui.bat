@echo off
rem Launch the flight watcher UI and open a browser at it.
rem Closing this window stops the server.

setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo   No virtualenv found at .venv
    echo   Run:  make install ^&^& make pw-install
    echo.
    pause
    exit /b 1
)

set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"

rem Open the browser once uvicorn has had a moment to bind the port. Backgrounded
rem so the server itself stays in the foreground and dies with this window.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start "" "%URL%""

echo.
echo   Flight watcher  ->  %URL%
echo   Close this window to stop the server.
echo.

"%PY%" -m uvicorn src.web.app:app --port %PORT%

rem Only reached if uvicorn exits on its own - keep the error readable.
if errorlevel 1 pause
endlocal
