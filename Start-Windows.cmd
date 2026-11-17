@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" >nul
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || python -m venv .venv || (echo Could not create a virtual environment. Install Python 3.10+ first. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo Could not install Streamlit. & pause & exit /b 1)
".venv\Scripts\python.exe" -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
pause
