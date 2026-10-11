@echo off
rem Консольный режим (для скриптов и агентов): dtf-backup sync --user petra | verify | render | status | check-api
rem Основной способ работы — LDTF.exe (значок в трее, своё окно)
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY call :find python
if not defined PY call :find py
if not defined PY (
  echo Не найден Python 3.12+: скачайте релиз LDTF-...-windows-x64.zip ^(в нём есть runtime^) или установите Python. 1>&2
  exit /b 9009
)
pushd "%~dp0"
"%PY%" -X utf8 -m dtf_backup %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

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
