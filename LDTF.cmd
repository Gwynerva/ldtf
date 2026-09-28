@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem LDTF: запуск без консольного окна, значок появится в трее. Для отладки: "LDTF (консоль).cmd"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" -X utf8 -m dtf_backup app %*
  exit /b 0
)
where pythonw >nul 2>nul && (
  start "" pythonw -X utf8 -m dtf_backup app %*
  exit /b 0
)
call "%~dp0LDTF (консоль).cmd" %*
