#!/bin/bash
# Simple startup script for the Streamlit frontend

# Set environment variables
export BACKEND_URL=${BACKEND_URL:-"https://smartai-backend-zefh.onrender.com"}

# Run Streamlit with proper configuration
streamlit run frontend/app.py \
  --server.port=8501 \
  --server.address=0.0.0.0 \
  --server.headless=true