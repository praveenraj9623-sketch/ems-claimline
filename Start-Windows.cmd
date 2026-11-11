@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.11 -m venv .venv || exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
".venv\Scripts\python.exe" -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
exit /b %errorlevel%
