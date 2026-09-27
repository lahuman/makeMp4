@echo off
setlocal
python -m pip install -r requirements-build.txt || exit /b 1
python build_release.py || exit /b 1
echo Build complete: release\MusicToVideo\MusicToVideo.exe
