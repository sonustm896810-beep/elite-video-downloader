@echo off
echo.
echo  ================================================
echo   Elite Video Downloader - Backend Server
echo  ================================================
echo.

cd /d "%~dp0backend"

echo  [1/3] Installing / upgrading dependencies...
pip install -r requirements.txt --quiet
echo.

echo  [2/3] Upgrading yt-dlp to latest version...
pip install --upgrade yt-dlp --quiet
echo.

echo  [3/3] Starting FastAPI server on http://localhost:8000
echo  API Docs at: http://localhost:8000/docs
echo  Press Ctrl+C to stop the server.
echo.

python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
pause
