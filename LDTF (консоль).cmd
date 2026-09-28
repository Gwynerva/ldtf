@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LDTF
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo.
  echo Не найден Python. Скачайте LDTF целиком, вместе с папкой runtime,
  echo или установите Python 3.12+ с https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
"%PY%" -X utf8 -m dtf_backup serve --open %*
if errorlevel 1 pause
