@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%src"

if exist "%ROOT%.venv\Scripts\python.exe" (
    "%ROOT%.venv\Scripts\python.exe" -m medlearn_vault.cli ui 2>nul
) else (
    py -3 -m medlearn_vault.cli ui 2>nul
)

if not errorlevel 1 goto :end
powershell.exe -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Host ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5ZCv5Yqo5aSx6LSl44CC6K+356Gu6K6kIFB5dGhvbiDlt7Llronoo4XvvIzlubbmo4Dmn6Xnq6/lj6MgODc2NSDmmK/lkKbooqvljaDnlKjjgII=')))"
pause

:end
endlocal
