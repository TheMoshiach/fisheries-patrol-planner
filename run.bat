@echo off
rem Starts the Fisheries Patrol Planner. First run creates the Python environment,
rem installs dependencies; later runs start straight away.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
        exit /b 1
    )
)

.venv\Scripts\python.exe -c "import patrol_planner, streamlit, streamlit_folium, pytest" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .venv\Scripts\python.exe -m pip install --quiet -e ".[app,dev]"
    if errorlevel 1 exit /b 1
)

echo Opening http://localhost:8501 ...
start "" /b cmd /c "%SystemRoot%\System32\timeout.exe /t 5 /nobreak >nul & start http://localhost:8501"
.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false
