@echo off
setlocal
cd /d "%~dp0"
where dotnet >nul 2>nul
if errorlevel 1 (
  echo ERROR: .NET 10 SDK not found. Install it from https://dotnet.microsoft.com/download/dotnet/10.0
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0package-portable.ps1" -Runtime win-x64
if errorlevel 1 (
  echo ERROR: WiFlow portable build failed.
  pause
  exit /b 1
)
echo.
echo WiFlow v1.0.0 portable: "%~dp0dist\WiFlow-Windows-x64-Portable-1.0.0.zip"
pause
