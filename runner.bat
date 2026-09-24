@echo off
setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker was not found. Install Docker Desktop and try again.
  exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
  echo Docker Compose v2 was not found. Verify Docker Desktop is running.
  exit /b 1
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo Created .env from .env.example. Edit it with your AWS Bedrock settings.
)

if not exist "artifacts" mkdir "artifacts"
if not exist "browser-profile" mkdir "browser-profile"
if not exist "agent-files" mkdir "agent-files"

docker compose up --build %*
endlocal
