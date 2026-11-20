@echo off
rem Opens the Storm Response Kit deployer page with ArcGIS Pro's Python.
rem Double-click it, or run it from Command Prompt or PowerShell. Extra
rem arguments are passed through, e.g. deploy.cmd --port 47700
setlocal
set "PROPY=%ProgramFiles%\ArcGIS\Pro\bin\Python\Scripts\propy.bat"
if not exist "%PROPY%" set "PROPY=%LocalAppData%\Programs\ArcGIS\Pro\bin\Python\Scripts\propy.bat"
if not exist "%PROPY%" (
  echo ArcGIS Pro's Python was not found. The deployer signs in through ArcGIS Pro.
  pause
  exit /b 1
)
rem propy prints a harmless "proj4-activate.bat is not recognized" line first.
call "%PROPY%" "%~dp0deployer\server.py" %*
if errorlevel 1 pause
