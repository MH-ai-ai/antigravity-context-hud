@echo off
setlocal
cd /d "%~dp0"

set "PYW="
for /f "delims=" %%I in ('python -c "import sys,os; p=os.path.join(os.path.dirname(sys.executable),'pythonw.exe'); print(p if os.path.isfile(p) else '')" 2^>nul') do set "PYW=%%I"

if defined PYW (
    start "" "%PYW%" "%~dp0hud.py"
) else (
    start "" python "%~dp0hud.py"
)
endlocal
