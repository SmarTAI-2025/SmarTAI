@echo off
REM Simple startup script for the Streamlit frontend on Windows

REM Set environment variables
set BACKEND_URL=https://smartai-backend-zefh.onrender.com

REM Run Streamlit with proper configuration
streamlit run frontend/app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true