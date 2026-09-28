@echo off
rem Double-click this. The laptop with no GitHub: it takes the newest release zip out of
rem Downloads (or Desktop, or the folder this sits in), installs it, and runs it. Nothing goes out.
cd /d "%~dp0"
call :findpy
if "%PY%"=="" goto nopython
%PY% launcher.py --source zip %*
if errorlevel 1 pause
exit /b
:findpy
for %%P in (py.exe python3.exe python.exe) do (
  for /f "delims=" %%I in ('where %%P 2^>nul') do ( set "PY=%%I" & goto :eof )
)
goto :eof
:nopython
echo Python 3 is needed once: https://python.org/downloads
echo Tick "Add python.exe to PATH" in the installer, then double-click this again.
pause
