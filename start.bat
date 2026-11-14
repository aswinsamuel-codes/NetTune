@echo off
rem NetTune launcher - starts the local engine and opens the dashboard.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m nettune %*
    goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python -m nettune %*
    goto :done
)

echo NetTune needs Python 3.9 or newer.
echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this file again.
pause

:done
endlocal
