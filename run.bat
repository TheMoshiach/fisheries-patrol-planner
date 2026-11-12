@echo off
rem Starts the Fisheries Patrol Planner and opens it in Google Chrome.
rem The first run creates the Python environment and installs dependencies.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

.venv\Scripts\python.exe -c "import patrol_planner, streamlit, streamlit_folium, pytest" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .venv\Scripts\python.exe -m pip install --quiet -e ".[app,dev]"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

rem Already running? Then just bring it up in the browser.
.venv\Scripts\python.exe tools\open_app.py --if-running
if not errorlevel 1 exit /b 0

echo Starting the Fisheries Patrol Planner. Keep this window open while you use it.
start "" /b .venv\Scripts\python.exe tools\open_app.py
.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false
