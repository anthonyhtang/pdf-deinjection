@echo off
setlocal

echo Syncing uv environment...
uv sync
if errorlevel 1 goto :fail

echo Generating icon...
uv run python icon_gen.py
if errorlevel 1 goto :fail

echo Building GUI EXE...
uv run pyinstaller --noconfirm --clean --onefile --windowed --icon=icon.ico --name="PDF Deinjection" --add-data "icon.ico;." --collect-data tkinterdnd2 --hidden-import=tkinterdnd2 main.py
if errorlevel 1 goto :fail

echo Building CLI EXE...
uv run pyinstaller --noconfirm --clean --onefile --console --icon=icon.ico --name="pdf-deinjection-cli" --add-data "icon.ico;." --collect-data tkinterdnd2 --hidden-import=tkinterdnd2 main.py
if errorlevel 1 goto :fail

echo Done. EXEs are in dist\
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1