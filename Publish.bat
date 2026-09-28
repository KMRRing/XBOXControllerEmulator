@echo off
rem Double-click this to cut a version. A window opens; nothing is typed at a prompt.
cd /d "%~dp0"
for %%P in (py.exe python3.exe python.exe) do (
  for /f "delims=" %%I in ('where %%P 2^>nul') do ( set "PY=%%I" & goto run )
)
echo Python 3 is needed once: https://python.org/downloads
pause
exit /b
:run
%PY% publish.py %*
if errorlevel 1 pause
