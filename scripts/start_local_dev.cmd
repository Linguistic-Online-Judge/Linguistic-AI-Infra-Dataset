@echo off
setlocal
title Linguistic Online Judge - Local Development
set "PROJECT_ROOT=%~dp0.."
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Project Python environment is missing.
    echo See docs\LOCAL_DEVELOPMENT.md to install the project dependencies.
    pause
    exit /b 1
)
echo Local website: http://127.0.0.1:8080/
echo Keep this window open while using the website.
echo This starts local mock evaluation, not the school Qwen service.
echo If the address already works, do not start a second copy.
echo.
"%PYTHON%" -m linguistic_oj.local_dev --root "%PROJECT_ROOT%" --port 8080
set "RESULT=%ERRORLEVEL%"
echo.
echo The local website process has stopped. Review any messages above.
echo It will not restart automatically after a computer reboot.
pause
exit /b %RESULT%
