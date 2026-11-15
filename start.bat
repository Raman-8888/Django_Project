@echo off
title Legal Document Redactor — Multi-Agent

echo.
echo ============================================================
echo   Legal Document Redactor  ^|  Multi-Agent Pipeline
echo ============================================================
echo.

:: ----------------------------------------------------------------
:: 1. Check Python
:: ----------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and try again.
    pause & exit /b 1
)
echo [1/4] Python OK

:: ----------------------------------------------------------------
:: 2. Install / update dependencies
:: ----------------------------------------------------------------
echo [2/4] Installing dependencies...
pip install -q -r django_app\requirements.txt
pip install -q google-genai
echo       Done.

:: ----------------------------------------------------------------
:: 3. Run Django migrations
:: ----------------------------------------------------------------
echo [3/4] Running migrations...
cd django_app
python manage.py migrate --run-syncdb -v 0
cd ..
echo       Done.

:: ----------------------------------------------------------------
:: 4. Start the server
:: ----------------------------------------------------------------
echo [4/4] Starting server at http://127.0.0.1:8000
echo.
echo       Press Ctrl+C to stop.
echo ============================================================
echo.
cd django_app
python manage.py runserver 8000
