@echo off
setlocal
set "PYTHONUTF8=1"
pushd "%~dp0" || exit /b 1
set "BUILD_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%BUILD_PYTHON%" goto install
where python >nul 2>&1
if errorlevel 1 goto use_launcher
python -m venv "%~dp0.venv"
if errorlevel 1 goto failed
goto install

:use_launcher
where py >nul 2>&1
if errorlevel 1 (
    echo Python 3.10 or newer is required. Install Python and run this script again.
    goto failed
)
py -3 -m venv "%~dp0.venv"
if errorlevel 1 goto failed

:install
"%BUILD_PYTHON%" -m pip install -r "%~dp0requirements-build.txt"
if errorlevel 1 goto failed
"%BUILD_PYTHON%" "%~dp0build_release.py"
if errorlevel 1 goto failed
echo Build complete: release\Seonyuldam\Seonyuldam.exe
popd
exit /b 0

:failed
echo Build failed. Review the error above. The existing release is preserved.
popd
exit /b 1
