@echo off
rem Build dist\kopy.exe with the kopy icon and Windows version info.
rem Run from this folder:  build_exe.bat
setlocal
cd /d "%~dp0"

where pyinstaller >nul 2>nul || (
    echo PyInstaller not found. Install it with:  pip install pyinstaller
    exit /b 1
)

pyinstaller --noconfirm --clean --onefile --console ^
    --name kopy ^
    --icon assets\kopy.ico ^
    --version-file version_info.txt ^
    kopy_cli.py || exit /b 1

echo.
echo Built dist\kopy.exe
dist\kopy.exe --version
