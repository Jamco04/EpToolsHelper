@echo off
rem Double-click to open the eptools window.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw eptools.pyw & exit /b)
where pythonw >nul 2>nul && (start "" pythonw eptools.pyw & exit /b)
echo Python was not found.
echo Install Python 3 from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or download eptools.exe from the Releases page instead.
pause
