@echo off
rem Консольный режим (для скриптов и агентов): dtf-backup sync --user petra | render | status | check-api
rem Основной способ работы — LDTF: "LDTF.cmd" (значок в трее)
set "PY=python"
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
pushd "%~dp0"
"%PY%" -X utf8 -m dtf_backup %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
