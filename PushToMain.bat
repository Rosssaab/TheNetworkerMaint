@echo off
REM Push TheNetworkerMaint to origin/main. Run while on branch main.
REM Usage: PushToMain.bat "Your commit message"
REM Log: logs\PushToMain-last.log
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "COMMIT_MSG=%~1"
set "LOG=%~dp0logs\PushToMain-last.log"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo Log file: %LOG%
echo.

> "%LOG%" echo === PushToMain %date% %time% ===
>> "%LOG%" echo Message: %~1
>> "%LOG%" echo.

call :run >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

>> "%LOG%" echo === Finished exit code %EXITCODE% ===

echo ========== OUTPUT (also in log file) ==========
type "%LOG%"
echo ===============================================
if "!EXITCODE!"=="0" (echo OK) else (echo FAILED - log: %LOG%)
pause
exit /b %EXITCODE%

:run
for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "BR=%%b"
if /i not "!BR!"=="main" (
  echo ERROR: git checkout main first
  exit /b 1
)

if not "!COMMIT_MSG!"=="" (
  if not defined TNW_SKIP_VERSION_BUMP call :bump_app_version patch
  call :drop_jinja_bytecode_from_git_index
  git add -A
  if exist ".env" git reset -q -- .env 2>nul
  if exist "GithubKeyPair" git reset -q -- GithubKeyPair 2>nul
  git reset -q -- "*.pem" 2>nul
  git diff --cached --quiet
  if errorlevel 1 (
    git commit -m "!COMMIT_MSG!"
    if errorlevel 1 exit /b 1
  ) else (
    echo Nothing new to commit - pushing existing commits.
  )
) else (
  git status --porcelain | findstr /r "." >nul 2>&1
  if not errorlevel 1 (
    echo ERROR: uncommitted changes - pass a commit message:
    echo   PushToMain.bat "Describe your changes"
    exit /b 1
  )
)

git push origin main
if errorlevel 1 exit /b 1
echo OK - pushed to https://github.com/Rosssaab/TheNetworkerMaint.git main
git log -1 --oneline
exit /b 0

:bump_app_version
set "BUMP_KIND=%~1"
for /f "delims=" %%v in ('python "%~dp0scripts\bump_version.py" !BUMP_KIND! 2^>nul') do set "TNW_NEW_VERSION=%%v"
if "!TNW_NEW_VERSION!"=="" (
  echo ERROR: could not bump APP_VERSION ^(python scripts\bump_version.py !BUMP_KIND!^)
  exit /b 1
)
echo APP_VERSION -^> !TNW_NEW_VERSION!
exit /b 0

:drop_jinja_bytecode_from_git_index
git ls-files instance/jinja_bytecode 2>nul | findstr /r "." >nul 2>&1
if not errorlevel 1 (
  echo Removing tracked Jinja bytecode from git index...
  git rm -r --cached -f instance/jinja_bytecode 2>nul
)
exit /b 0
