@echo off
REM Push TheNetworkerMaint to https://github.com/Rosssaab/TheNetworkerMaint (branch main).
REM Usage: PushToMaint.bat "Your commit message"
REM Log: logs\PushToMaint-last.log
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "COMMIT_MSG=%~1"
set "LOG=%~dp0logs\PushToMaint-last.log"
set "REPO_URL=https://github.com/Rosssaab/TheNetworkerMaint.git"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo Log file: %LOG%
echo.

> "%LOG%" echo === PushToMaint %date% %time% ===
>> "%LOG%" echo Message: %~1
>> "%LOG%" echo.

call :run >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

>> "%LOG%" echo === Finished exit code %EXITCODE% ===

echo ========== OUTPUT (also in log file) ==========
type "%LOG%"
echo ===============================================
if "!EXITCODE!"=="0" (echo OK) else (echo FAILED - log: %LOG%)
if not defined TNW_NO_PAUSE pause
exit /b %EXITCODE%

:run
call :ensure_origin
if errorlevel 1 exit /b 1

for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "BR=%%b"
if /i not "!BR!"=="main" (
  echo ERROR: git checkout main first
  exit /b 1
)

git branch --set-upstream-to=origin/main main >nul 2>&1

if not "!COMMIT_MSG!"=="" (
  if not defined TNW_SKIP_VERSION_BUMP call :bump_app_version patch
  call :drop_jinja_bytecode_from_git_index
  git add -A
  if exist ".env" git reset -q -- .env 2>nul
  if exist "GithubKeyPair" git reset -q -- GithubKeyPair 2>nul
  git reset -q -- "*.pem" 2>nul
  if exist "deploy\maint-staging-ssh.local.env" git reset -q -- "deploy/maint-staging-ssh.local.env" 2>nul
  if exist "deploy\staging-ssh.local.env" git reset -q -- "deploy/staging-ssh.local.env" 2>nul
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
    echo   PushToMaint.bat "Describe your changes"
    exit /b 1
  )
)

git push -u origin main
if errorlevel 1 exit /b 1
echo OK - pushed to %REPO_URL% main
git log -1 --oneline
exit /b 0

:ensure_origin
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo ERROR: not a git repository
  exit /b 1
)
for /f "delims=" %%r in ('git remote get-url origin 2^>nul') do set "ORIGIN=%%r"
if /i not "!ORIGIN!"=="%REPO_URL%" (
  echo Setting origin to %REPO_URL% ^(was: !ORIGIN!^)
  git remote set-url origin %REPO_URL%
  if errorlevel 1 exit /b 1
)
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
