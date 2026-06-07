@echo off
title Digital Twin Launcher
echo =======================================================
echo 🚀 Launching Operational Digital Twin Pipeline...
echo =======================================================

:: 1. Start the Streamlit Dashboard in a new window
echo Starting Streamlit Dashboard...
start "Dashboard - Do Not Close" cmd /k "call venv\Scripts\activate && python -m streamlit run live_dashboard3.py"

:: Wait 3 seconds to let the Dashboard initialize
timeout /t 3 /nobreak >nul

:: 2. Start the Physics Engine in a new window
:: UPDATED: Now pointing inside the Scripts folder
echo Starting Digital Twin Engine...
start "Physics Engine - Press Ctrl+C Here To Stop" cmd /k "call venv\Scripts\activate && python Scripts\16_op_twin1.py"

echo =======================================================
echo ✅ Both processes are running in separate windows!
echo =======================================================
exit