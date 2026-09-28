@echo off
rem The online laptop needs this file and nothing else. On the first double-click it asks for your GitHub
rem token, fetches launcher.py out of the repository, and runs it. After that the launcher keeps itself,
rem the app and the data up to date, and this file never asks again.
rem
rem It also works unchanged inside an unpacked release zip, where launcher.py is already next to it.
cd /d "%~dp0"

rem --- identity: publish.py writes this block from app/appinfo.json ---------------------------------------
set "APP_NAME=XBOXControllerEmulator"
set "APP_REPO=KMRRing/XBOXControllerEmulator"
rem --- end identity --------------------------------------------------------------------------------------

set "HOME_DIR=%LOCALAPPDATA%\%APP_NAME%"
set "PY="
call :findpy
if not defined PY call :getpython
if not defined PY goto nopython
if exist "launcher.py" goto run

echo.
echo   %APP_NAME% lives in the private repository %APP_REPO%.
echo   Paste your GitHub token - fine-grained, Contents: read and write.
echo   It is asked for once, and kept in %HOME_DIR%.
echo.
set /p "TOKEN=  Token: "
if not defined TOKEN goto notoken
echo.
echo   Fetching the launcher...
curl -fsSL -H "Authorization: Bearer %TOKEN%" -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/%APP_REPO%/contents/launcher.py" -o "launcher.py"
if errorlevel 1 goto nofetch
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%"
>"%HOME_DIR%\token.txt" echo %TOKEN%

:run
"%PY%" launcher.py %*
if errorlevel 1 pause
exit /b

:findpy
for %%P in (py.exe python3.exe python.exe) do (
  for /f "delims=" %%I in ('where %%P 2^>nul') do ( set "PY=%%I" & goto :eof )
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" ( set "PY=%%D\python.exe" & goto :eof )
)
goto :eof

:getpython
echo.
echo   Python 3 is needed once. Installing it...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
call :findpy
if not defined PY echo   That did not work on its own.
goto :eof

:notoken
echo.
echo   No token, so there is nothing to fetch. Run this again when you have one.
pause
exit /b 1

:nofetch
if exist "launcher.py" del "launcher.py"
echo.
echo   Could not fetch the launcher. Either the token is wrong, or it cannot see %APP_REPO%,
echo   or this machine cannot reach github.com. Check the token and run this again.
pause
exit /b 1

:nopython
echo.
echo   Python 3 is needed once: https://python.org/downloads
echo   Tick "Add python.exe to PATH" in the installer, then double-click this again.
pause
exit /b 1
