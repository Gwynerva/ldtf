@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem LDTF from a git checkout: no console window, the icon appears in the tray. Debugging: "LDTF (консоль).cmd".
rem (The release starts with LDTF.exe.)
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" -X utf8 -m dtf_backup app %*
  exit /b 0
)
set "PY="
call :find pythonw
if defined PY (
  start "" "%PY%" -X utf8 -m dtf_backup app %*
  exit /b 0
)
call "%~dp0LDTF (консоль).cmd" %*
exit /b %ERRORLEVEL%

:find
rem %1 = python | py: the first one on PATH that really runs and is 3.12+. Not found: PY stays empty. The Microsoft
rem Store alias (WindowsApps\python.exe without Python installed) fails this check instead of printing "Python ..."
for /f "delims=" %%P in ('where %1 2^>nul') do (
  "%%P" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && (
    set "PY=%%P"
    exit /b 0
  )
)
exit /b 0
