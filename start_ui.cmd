@echo off
rem Starts the OEE Validator screens on this PC only (http://localhost:8501).
rem Run from anywhere; it switches to the folder that contains oee_validator\.
cd /d "%~dp0.."
python -m streamlit run oee_validator\ui.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false
