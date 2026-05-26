@echo off
REM 1) Commit/push main if needed (PushToMaint.bat)
REM 2) Merge main -^> staging and push to GitHub
REM 3) SSH to Linux and run deploy/maint-staging-deploy.sh
REM
REM Optional: PushToMaintStaging.bat "commit message"
REM One-time setup: copy deploy\maint-staging-ssh.local.env.example to deploy\maint-staging-ssh.local.env
REM Log: logs\PushToMaintStaging-last.log
setlocal EnableExtensions EnableDelayedExpansion

set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"
set "COMMIT_MSG=%~1"
set "TNW_NO_PAUSE=1"
set "RUN=%TEMP%\tnw-PushToMaintStaging.cmd"
if /i not "%~f0"=="%RUN%" (
  copy /y "%~f0" "%RUN%" >nul
  call "%RUN%" "%PROJECT%" "!COMMIT_MSG!"
  exit /b %ERRORLEVEL%
)

set "PROJECT=%~1"
set "COMMIT_MSG=%~2"
cd /d "%PROJECT%"
if errorlevel 1 (
  echo ERROR: could not cd to repo: %PROJECT%
  exit /b 1
)

set "LOG=%PROJECT%\logs\PushToMaintStaging-last.log"
set "ENV_FILE=%PROJECT%\deploy\maint-staging-ssh.local.env"
if not exist "%PROJECT%\logs" mkdir "%PROJECT%\logs"

echo Log file: %LOG%
echo.

for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "BR=%%b"
if /i not "!BR!"=="main" (
  echo ERROR: switch to main first:  git checkout main
  exit /b 1
)

call :ensure_main_pushed
if errorlevel 1 exit /b 1

call :bump_version_minor_on_main
if errorlevel 1 exit /b 1

if not exist "%ENV_FILE%" (
  echo ERROR: missing %ENV_FILE%
  echo Copy deploy\maint-staging-ssh.local.env.example to deploy\maint-staging-ssh.local.env
  echo and set STAGING_SSH_HOST, STAGING_SSH_USER, STAGING_SSH_KEY.
  exit /b 1
)

set "STAGING_SSH_HOST="
set "STAGING_SSH_USER="
set "STAGING_SSH_KEY="
set "STAGING_APP_DIR="
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%ENV_FILE%") do (
  if /i "%%a"=="STAGING_SSH_HOST" set "STAGING_SSH_HOST=%%b"
  if /i "%%a"=="STAGING_SSH_USER" set "STAGING_SSH_USER=%%b"
  if /i "%%a"=="STAGING_SSH_KEY" set "STAGING_SSH_KEY=%%b"
  if /i "%%a"=="STAGING_APP_DIR" set "STAGING_APP_DIR=%%b"
)
set "STAGING_SSH_HOST=!STAGING_SSH_HOST: =!"
set "STAGING_SSH_USER=!STAGING_SSH_USER: =!"
set "STAGING_SSH_KEY=!STAGING_SSH_KEY: =!"
set "STAGING_APP_DIR=!STAGING_APP_DIR: =!"
if "!STAGING_APP_DIR!"=="" set "STAGING_APP_DIR=/home/ubuntu/PythonRoot/maint"

if "!STAGING_SSH_HOST!"=="" (
  echo ERROR: STAGING_SSH_HOST not set in %ENV_FILE%
  exit /b 1
)
if "!STAGING_SSH_USER!"=="" (
  echo ERROR: STAGING_SSH_USER not set in %ENV_FILE%
  exit /b 1
)
if "!STAGING_SSH_KEY!"=="" (
  echo ERROR: STAGING_SSH_KEY not set in %ENV_FILE%
  exit /b 1
)
if not exist "!STAGING_SSH_KEY!" (
  echo ERROR: SSH key not found: !STAGING_SSH_KEY!
  exit /b 1
)

echo Push maint staging to GitHub and deploy to server...
> "%LOG%" echo === PushToMaintStaging %date% %time% ===
>> "%LOG%" echo Repo: %PROJECT%
>> "%LOG%" echo Server: !STAGING_SSH_USER!@!STAGING_SSH_HOST!
>> "%LOG%" echo.

call :git_push >> "%LOG%" 2>&1
if errorlevel 1 (
  call :show_log_fail 1
  exit /b 1
)

call :ssh_deploy >> "%LOG%" 2>&1
if errorlevel 1 (
  call :show_log_fail 1
  exit /b 1
)

>> "%LOG%" echo.
>> "%LOG%" echo === All OK ===
call :show_log_ok
exit /b 0

:git_push
echo --- Git: merge main into staging and push ---
git merge --abort 2>nul
git push origin main
if errorlevel 1 exit /b 1
call :discard_generated_caches
git fetch origin staging 2>nul
git show-ref --verify --quiet refs/remotes/origin/staging
if errorlevel 1 (
  echo Creating local staging branch from main...
  git checkout -b staging
) else (
  git checkout staging
  if errorlevel 1 exit /b 1
  git pull origin staging
  if errorlevel 1 exit /b 1
)
call :discard_generated_caches
call :drop_jinja_bytecode_from_git_index
call :drop_upload_static_from_git_index
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Remove Jinja bytecode from version control"
  if errorlevel 1 exit /b 1
)
git merge main -m "Merge main into staging"
if errorlevel 1 (
  call :resolve_jinja_merge_conflicts
  if errorlevel 1 exit /b 1
)
git push -u origin staging
if errorlevel 1 exit /b 1
git checkout main
if errorlevel 1 exit /b 1
echo OK - origin/staging updated on TheNetworkerMaint
git log -1 --oneline
exit /b 0

:ssh_deploy
echo.
echo --- SSH: deploy maint app on server ---
set "SSH_TARGET=!STAGING_SSH_USER!@!STAGING_SSH_HOST!"
set "GIT_CHK=no"
echo Checking for git repo on server...
for /f "delims=" %%g in ('ssh -i "!STAGING_SSH_KEY!" -o BatchMode^=yes -o StrictHostKeyChecking^=accept-new "!SSH_TARGET!" "test -d '!STAGING_APP_DIR!/.git' && echo yes || echo no"') do set "GIT_CHK=%%g"
if /i "!GIT_CHK!"=="yes" (
  echo Using git fetch on server...
  ssh -i "!STAGING_SSH_KEY!" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "!SSH_TARGET!" "cd '!STAGING_APP_DIR!' && GIT_TERMINAL_PROMPT=0 git fetch origin staging && git reset --hard origin/staging && TNW_STAGING_APP_DIR='!STAGING_APP_DIR!' TNW_DEPLOY_REEXEC=1 bash deploy/maint-staging-deploy.sh"
  if errorlevel 1 goto :ssh_deploy_fail
  goto :ssh_deploy_ok
)

echo No git repo on server - syncing origin/staging via archive...
set "DEPLOY_TAR=%TEMP%\tnw-maint-staging-deploy-%RANDOM%.tar"
if exist "!DEPLOY_TAR!" del /f /q "!DEPLOY_TAR!"
echo Creating archive from origin/staging...
git archive --format=tar -o "!DEPLOY_TAR!" origin/staging
if errorlevel 1 (
  echo ERROR: git archive origin/staging failed
  if exist "!DEPLOY_TAR!" del /f /q "!DEPLOY_TAR!"
  goto :ssh_deploy_fail
)
if not exist "!DEPLOY_TAR!" (
  echo ERROR: archive file was not created: !DEPLOY_TAR!
  goto :ssh_deploy_fail
)

echo Uploading archive to server...
ssh -i "!STAGING_SSH_KEY!" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "!SSH_TARGET!" "mkdir -p '!STAGING_APP_DIR!'"
if errorlevel 1 goto :ssh_deploy_archive_cleanup_fail

scp -i "!STAGING_SSH_KEY!" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "!DEPLOY_TAR!" "!SSH_TARGET!:/tmp/tnw-maint-staging-deploy.tar"
if errorlevel 1 goto :ssh_deploy_archive_cleanup_fail

echo Extracting on server and running maint-staging-deploy.sh...
ssh -i "!STAGING_SSH_KEY!" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "!SSH_TARGET!" "bash -lc 'set -e; APP=\"!STAGING_APP_DIR!\"; if [ -f \"$APP/.env\" ]; then cp \"$APP/.env\" /tmp/tnw-maint.env.bak; fi; rm -rf \"$APP\"; mkdir -p \"$APP\"; tar -xf /tmp/tnw-maint-staging-deploy.tar -C \"$APP\"; rm -f /tmp/tnw-maint-staging-deploy.tar; find \"$APP/deploy\" -name \"*.sh\" -exec sed -i \"s/\\r$//\" {} + 2>/dev/null || true; if [ -f /tmp/tnw-maint.env.bak ]; then mv /tmp/tnw-maint.env.bak \"$APP/.env\"; fi; cd \"$APP\" && TNW_STAGING_APP_DIR=\"$APP\" TNW_DEPLOY_REEXEC=1 bash deploy/maint-staging-deploy.sh'"
if errorlevel 1 goto :ssh_deploy_archive_cleanup_fail

del /f /q "!DEPLOY_TAR!" 2>nul
goto :ssh_deploy_ok

:ssh_deploy_archive_cleanup_fail
if exist "!DEPLOY_TAR!" del /f /q "!DEPLOY_TAR!" 2>nul
goto :ssh_deploy_fail

:ssh_deploy_fail
echo ERROR: SSH deploy failed.
exit /b 1

:ssh_deploy_ok
echo OK - server deploy finished
exit /b 0

:show_log_ok
echo ========== OUTPUT ==========
type "%LOG%"
echo ============================
echo OK - maint staging deploy complete
echo Log: %LOG%
exit /b 0

:show_log_fail
echo ========== OUTPUT ==========
type "%LOG%"
echo ============================
echo FAILED - log: %LOG%
exit /b %1

:discard_generated_caches
git restore --worktree --staged instance/jinja_bytecode 2>nul
git clean -fd instance/jinja_bytecode 2>nul
exit /b 0

:drop_jinja_bytecode_from_git_index
git ls-files instance/jinja_bytecode 2>nul | findstr /r "." >nul 2>&1
if not errorlevel 1 (
  echo Removing tracked Jinja bytecode from git index...
  git rm -r --cached -f instance/jinja_bytecode 2>nul
)
exit /b 0

:drop_upload_static_from_git_index
for %%D in (meeting_group_images event_images user_images) do (
  git ls-files "app/static/%%D" 2>nul | findstr /r "." >nul 2>&1
  if not errorlevel 1 (
    echo Removing tracked upload files from git index: app/static/%%D
    git rm -r --cached -f "app/static/%%D" 2>nul
  )
)
exit /b 0

:resolve_jinja_merge_conflicts
git ls-files -u instance/jinja_bytecode 2>nul | findstr /r "." >nul 2>&1
if errorlevel 1 exit /b 1
echo Resolving Jinja bytecode merge conflicts by removing from git...
git rm -r -f instance/jinja_bytecode 2>nul
git commit -m "Merge main into staging"
if errorlevel 1 exit /b 1
exit /b 0

:bump_version_minor_on_main
for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "BR=%%b"
if /i not "!BR!"=="main" (
  echo ERROR: version bump requires branch main
  exit /b 1
)
echo --- Bump APP_VERSION minor on main ---
for /f "delims=" %%v in ('python "%PROJECT%\scripts\bump_version.py" minor 2^>nul') do set "TNW_NEW_VERSION=%%v"
if "!TNW_NEW_VERSION!"=="" (
  echo ERROR: could not bump APP_VERSION minor
  exit /b 1
)
echo APP_VERSION -^> !TNW_NEW_VERSION!
git add config.py
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Version !TNW_NEW_VERSION!"
  if errorlevel 1 exit /b 1
  git push origin main
  if errorlevel 1 exit /b 1
)
exit /b 0

:ensure_main_pushed
set "NEED_PUSH=0"
git status --porcelain | findstr /r "." >nul 2>&1
if not errorlevel 1 set "NEED_PUSH=1"
if "!NEED_PUSH!"=="0" (
  git rev-parse --verify "@{u}" >nul 2>&1
  if not errorlevel 1 (
    for /f %%c in ('git rev-list --count "@{u}"..HEAD 2^>nul') do if %%c GTR 0 set "NEED_PUSH=1"
  )
)
if "!NEED_PUSH!"=="0" exit /b 0
if "!COMMIT_MSG!"=="" set "COMMIT_MSG=Maint staging deploy %date% %time%"
echo --- Main has unpushed or uncommitted work; running PushToMaint first ---
echo Commit message: !COMMIT_MSG!
echo.
set "TNW_SKIP_VERSION_BUMP=1"
call "%PROJECT%\PushToMaint.bat" "!COMMIT_MSG!"
set "TNW_SKIP_VERSION_BUMP="
if errorlevel 1 (
  echo ERROR: PushToMaint failed - fix main and retry.
  exit /b 1
)
echo.
exit /b 0
