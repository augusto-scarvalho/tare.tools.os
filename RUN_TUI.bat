@echo off
rem Open the explicitly managed release. A source install runs tare from its own virtual environment.
setlocal EnableExtensions DisableDelayedExpansion
if not defined TARE_PRODUCT_HOME set "TARE_PRODUCT_HOME=%LOCALAPPDATA%\tare.tools\product"
if not exist "%TARE_PRODUCT_HOME%\tare.cmd" (
    echo [RUN_TUI] Install a product bundle first: python install.py install . 1>&2
    echo [RUN_TUI] Expected launcher: "%TARE_PRODUCT_HOME%\tare.cmd" 1>&2
    exit /b 1
)
for %%I in ("%~dp0.") do set "TARE_ROOT=%%~fI"
call "%TARE_PRODUCT_HOME%\tare.cmd" console --root "%TARE_ROOT%" %*
exit /b %ERRORLEVEL%
