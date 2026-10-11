@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LDTF
rem LDTF in a console window (from a git checkout; the release starts with LDTF.exe). Stop: Ctrl+C.
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY call :find python
if not defined PY call :find py
if not defined PY (
  echo.
  echo Не найден Python 3.12 или новее.
  echo.
  echo  * Обычный способ: скачайте с GitHub файл LDTF-...-windows-x64.zip ^(не «Source code»^),
  echo    распакуйте его целиком и запустите LDTF.exe — Python уже внутри, в папке runtime.
  echo  * Чтобы запускать из исходников: установите Python 3.12+ с https://www.python.org/downloads/
  echo    ^(ярлык «python» из Microsoft Store без установленного Python не подходит^).
  echo.
  pause
  exit /b 1
)
"%PY%" -X utf8 -m dtf_backup serve --open %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo LDTF завершился с ошибкой ^(код %RC%^). Подробности — выше и в archive\.state\app.log
  pause
)
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
