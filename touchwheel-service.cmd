@echo off
setlocal EnableDelayedExpansion

rem touchwheel background launcher.
rem
rem Deliberately NOT a Windows service: services run in session 0, which cannot
rem see the desktop's windows and cannot inject input into them. SendInput,
rem PostMessage and the Raw Input sink all have to live in the interactive
rem session.
rem
rem Autostart goes through the per-user Run key rather than a scheduled task,
rem because schtasks /SC ONLOGON requires elevation and this needs none.

set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
set "RUN_NAME=touchwheel"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%touchwheel.py"
set "ARGS=--no-park"

if not exist "%SCRIPT%" (
    echo ERROR: touchwheel.py not found next to this script.
    exit /b 1
)

rem No argument means a person ran it, most likely by double-clicking: show the
rem menu. Named actions still work for scripting.
set "ACTION=%~1"
if "%ACTION%"=="" goto :menu

if /i "%ACTION%"=="install"   goto :install
if /i "%ACTION%"=="uninstall" goto :uninstall
if /i "%ACTION%"=="remove"    goto :uninstall
if /i "%ACTION%"=="start"     goto :start
if /i "%ACTION%"=="stop"      goto :stop
if /i "%ACTION%"=="status"    goto :status
if /i "%ACTION%"=="menu"      goto :menu
goto :help


:menu
cls
echo ===============================================================
echo   touchwheel  -  touch scrolling for Windows Terminal
echo ===============================================================
echo.
call :show_state
echo.
echo   [1]  Install autostart  ^(and start now^)
echo.
echo   [2]  Start
echo   [3]  Stop
echo   [4]  Status
echo.
echo   [5]  Remove autostart   ^(and stop^)
echo   [Q]  Quit
echo.
set "CHOICE="
set /p "CHOICE=Select: "
rem Every option is one character. Truncating also discards a trailing CR,
rem which set /p keeps when stdin is a pipe with Unix line endings.
if defined CHOICE set "CHOICE=!CHOICE:~0,1!"

if /i "!CHOICE!"=="1" goto :m_install
if /i "!CHOICE!"=="2" goto :m_start
if /i "!CHOICE!"=="3" goto :m_stop
if /i "!CHOICE!"=="4" goto :m_status
if /i "!CHOICE!"=="5" goto :m_remove
if /i "!CHOICE!"=="q" exit /b 0
rem An empty answer means Enter, which refreshes. It also means EOF when stdin
rem is a closed pipe, so give up rather than redraw forever.
if /i "!CHOICE!"=="" (
    set /a EMPTY+=1
    if !EMPTY! geq 3 exit /b 0
    goto :menu
)
set "EMPTY=0"
echo.
echo Not an option: !CHOICE!
goto :menu_pause

:m_install
echo.
call :install
goto :menu_pause

:m_start
echo.
call :start
goto :menu_pause

:m_stop
echo.
call :stop
goto :menu_pause

:m_status
echo.
call :status
goto :menu_pause

:m_remove
echo.
call :uninstall
goto :menu_pause


:menu_pause
rem set /p rather than pause: pause reads the console directly, which makes the
rem menu impossible to drive from a script or a pipe.
echo.
set "DUMMY="
set /p "DUMMY=Press Enter to continue . . . "
goto :menu


:show_state
reg query "%RUN_KEY%" /v "%RUN_NAME%" >nul 2>&1
if errorlevel 1 (
    echo   Autostart : NOT installed
) else (
    echo   Autostart : installed, runs at logon
)
for /f "delims=" %%S in ('powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*touchwheel.py*' }; if ($p) { 'RUNNING  (PID ' + (($p | ForEach-Object { $_.ProcessId }) -join ', ') + ')' } else { 'not running' }" 2^>nul ^<nul') do set "PROC_STATE=%%S"
if not defined PROC_STATE set "PROC_STATE=unknown"
echo   Process   : !PROC_STATE!
set "PROC_STATE="
exit /b 0


:resolve_pythonw
rem Find the interpreter uv would use, then its windowless twin, so nothing
rem shows a console window and no uv wrapper process sits in the tree.
for /f "delims=" %%P in ('uv run --no-project python -c "import sys,pathlib;print(pathlib.Path(sys.executable).with_name('pythonw.exe'))" 2^>nul ^<nul') do set "PYTHONW=%%P"
if not defined PYTHONW (
    echo ERROR: could not resolve a Python interpreter through uv.
    echo Check that uv is installed and on PATH.
    exit /b 1
)
if not exist "!PYTHONW!" (
    echo ERROR: pythonw.exe not found at !PYTHONW!
    exit /b 1
)
exit /b 0


:install
call :resolve_pythonw || exit /b 1
echo Interpreter: !PYTHONW!
reg add "%RUN_KEY%" /v "%RUN_NAME%" /t REG_SZ /f ^
    /d "\"!PYTHONW!\" \"%SCRIPT%\" %ARGS%" >nul
if errorlevel 1 (
    echo ERROR: could not write the Run key.
    exit /b 1
)
echo Installed. touchwheel will start at every logon.
call :start
exit /b 0


:uninstall
call :stop_quiet
reg query "%RUN_KEY%" /v "%RUN_NAME%" >nul 2>&1
if errorlevel 1 (
    echo Autostart was not installed.
    exit /b 0
)
reg delete "%RUN_KEY%" /v "%RUN_NAME%" /f >nul
echo Removed autostart.
exit /b 0


:start
call :resolve_pythonw || exit /b 1
rem touchwheel holds a single-instance mutex, so a second copy exits by itself.
start "" "!PYTHONW!" "%SCRIPT%" %ARGS%
echo Started.
exit /b 0


:stop
call :stop_quiet
echo Stopped.
exit /b 0


:stop_quiet
rem wmic is removed on current Windows 11 builds, so process lookup goes
rem through PowerShell's CIM cmdlets.
powershell -NoProfile -Command ^
    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*touchwheel.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" <nul >nul 2>&1
exit /b 0


:status
reg query "%RUN_KEY%" /v "%RUN_NAME%" >nul 2>&1
if errorlevel 1 (
    echo Autostart: not installed
) else (
    echo Autostart: installed
)
echo.
echo Running processes:
powershell -NoProfile -Command ^
    "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*touchwheel.py*' }; if ($p) { $p | ForEach-Object { '  PID ' + $_.ProcessId } } else { '  none' }" <nul
exit /b 0


:help
echo touchwheel background launcher
echo.
echo   %~nx0              interactive menu (also what double-clicking does)
echo   %~nx0 install     start at every logon, and start now
echo   %~nx0 uninstall   stop it and remove autostart
echo   %~nx0 start       start it now
echo   %~nx0 stop        stop it now
echo   %~nx0 status      show autostart state and running processes
echo.
echo Not a Windows service by design: a service runs in session 0 and cannot
echo inject input into your desktop session. This runs in your own session
echo via the per-user Run key, which needs no administrator rights.
exit /b 0
