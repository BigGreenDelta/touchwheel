@echo off
rem touchwheel launcher. Double-click it: that opens the Textual settings UI,
rem which is where autostart, start/stop and every option already live.
rem
rem There is no command-line surface here on purpose. To drive the shim by
rem hand, run it by hand:
rem     uv run --no-project python touchwheel.py --help
rem
rem Autostart is deliberately not a Windows service: services run in session 0,
rem which cannot see the desktop's windows and cannot inject input into them --
rem SendInput, PostMessage and the Raw Input sink all have to live in the
rem interactive session. The UI installs a per-user Run key instead, which also
rem needs no elevation, unlike schtasks /SC ONLOGON.

set "SCRIPT_DIR=%~dp0"
set "TUI=%SCRIPT_DIR%touchwheel_tui.py"

if not exist "%TUI%" (
    echo ERROR: touchwheel_tui.py not found next to this script.
    pause
    exit /b 1
)

rem Textual is pulled in on demand; it is not a dependency of the shim itself.
uv run --with textual --no-project python "%TUI%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo The settings UI could not start ^(exit code %RC%^).
    echo Check that uv is installed and on PATH, or run the shim directly:
    echo     uv run --no-project python "%SCRIPT_DIR%touchwheel.py"
    echo.
    pause
)
exit /b %RC%
