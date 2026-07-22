@echo off
rem ===================================================================
rem  EliteOps dashboard launcher (Route/Navigate/System/Exo/Guardian/
rem  Shipwright/Firsts). Double-click to start the local server + open it.
rem  The app is at E:\Projects\EliteOps and does NOT require EDDiscovery.
rem ===================================================================
title EliteOps
cd /d "E:\Projects\EliteOps"

where python >nul 2>nul && (set "PY=python") || (set "PY=py")

echo Starting EliteOps dashboard...
start "EliteOps server" /min cmd /c "%PY% run.py"

rem detect this PC's primary LAN IPv4 (for tablets/phones)
set "LANIP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  if not defined LANIP for /f "tokens=* delims= " %%b in ("%%a") do set "LANIP=%%b"
)

timeout /t 2 >nul
start "" "http://localhost:8384"

echo.
echo  ==================================================================
echo    EliteOps is running.
echo.
echo    On THIS PC:       http://localhost:8384
if defined LANIP echo    On tablet/phone:  http://%LANIP%:8384
if not defined LANIP echo    On tablet/phone:  http://^<this-PC-IP^>:8384  (run ipconfig)
echo.
echo    Tablet notes: same Wi-Fi as this PC, and if it won't connect,
echo    allow inbound TCP 8384 in Windows Firewall (Private profile).
echo.
echo    To STOP: close the minimized "EliteOps server" window.
echo  ==================================================================
echo.
timeout /t 10 >nul
