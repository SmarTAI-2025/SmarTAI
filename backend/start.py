#!/usr/bin/env python3
"""
Startup script for the SmarTAI backend.
This script ensures the correct Python path is set before starting the application.
"""
import sys
import os

# Add the project root to the Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Now import and run the main application
from backend.main import app

if __name__ == "__main__":
    import uvicorn
    import logging
    import random
    
    # Set up logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    # Get port from environment variable or use random port
    port = int(os.environ.get("PORT", random.randint(8000, 9000)))
    
    logger.info(f"Starting FastAPI backend service on http://localhost:{port}")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, log_level="info")