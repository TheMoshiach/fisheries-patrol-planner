@echo off
rem Runs the test suite.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
.venv\Scripts\python.exe -c "import patrol_planner, streamlit_folium, pytest" 2>nul
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .venv\Scripts\python.exe -m pip install --quiet -e ".[app,dev]"
)
.venv\Scripts\python.exe -m pytest %*
