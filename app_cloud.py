"""
SmarTAI - Combined Frontend and Backend for Streamlit Cloud Deployment

This single-file application combines both the FastAPI backend and Streamlit frontend
to run as a single process on Streamlit Cloud by directly importing and running
the original main files.
"""

import os
import sys
import threading
import time
import logging
import signal
import atexit
import subprocess

# Add the project root to the path so we can import backend modules
# We need to add the parent directory of frontend to access backend which is a sibling directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI Backend Setup ---
from fastapi import FastAPI
import uvicorn

# Import the original backend app
from backend.main import app as backend_app

# Global variables for thread management
server_manager = None
shutdown_event = threading.Event()

def run_backend():
    """Run the original FastAPI backend app"""
    try:
        # Configure uvicorn to run on port 8000 to match frontend expectations
        config = uvicorn.Config(
            app=backend_app,
            host="127.0.0.1",
            port=8000,  # Use port 8000 to match frontend configuration
            log_level="info"
        )
        server = uvicorn.Server(config)
        
        # Run the server
        logger.info("Starting backend server on port 8000")
        server.run()
    except Exception as e:
        logger.error(f"Backend server error: {e}")
    finally:
        logger.info("Backend server thread finished")

def run_frontend():
    """Run the original Streamlit frontend app using subprocess"""
    try:
        # Use subprocess to run Streamlit properly
        logger.info("Starting frontend app with Streamlit")
        frontend_path = os.path.join(os.path.dirname(__file__), "frontend", "main.py")
        cmd = [sys.executable, "-m", "streamlit", "run", frontend_path, "--server.port", "8501"]
        process = subprocess.Popen(cmd)
        logger.info(f"Frontend Streamlit app started with PID {process.pid}")
        return process
    except Exception as e:
        logger.error(f"Frontend app error: {e}")
        return None

# --- Server Management ---
class ServerManager:
    def __init__(self):
        self.backend_thread = None
        self.backend_running = False
        self.frontend_process = None
        
    def start_backend(self):
        """Start the FastAPI backend in a separate thread"""
        global shutdown_event
        if not self.backend_running:
            self.backend_running = True
            self.backend_thread = threading.Thread(target=run_backend, daemon=True)
            self.backend_thread.start()
            logger.info("Backend server started in thread")
            
    def start_frontend(self):
        """Start the Streamlit frontend as a subprocess"""
        self.frontend_process = run_frontend()
        if self.frontend_process:
            logger.info("Frontend server started as subprocess")
            
    def stop_backend(self):
        """Stop the FastAPI backend"""
        self.backend_running = False
        if self.backend_thread and self.backend_thread.is_alive():
            logger.info("Waiting for backend thread to finish...")
            # Note: We can't forcefully stop the uvicorn server thread
            # The thread will stop when the main process exits
        logger.info("Backend server stopped")
        
    def stop_frontend(self):
        """Stop the Streamlit frontend"""
        if self.frontend_process:
            logger.info("Terminating frontend process...")
            self.frontend_process.terminate()
            try:
                self.frontend_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.frontend_process.kill()
            logger.info("Frontend process terminated")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()

def cleanup():
    """Cleanup function to be called on exit"""
    global server_manager
    if server_manager:
        server_manager.stop_frontend()
        server_manager.stop_backend()
    logger.info("Cleanup completed")

# Global server manager instance
server_manager = ServerManager()

# Register cleanup function
atexit.register(cleanup)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Start backend when this file is run directly
if __name__ == "__main__":
    try:
        # Start the backend server
        server_manager.start_backend()
        
        # Give the backend a moment to start
        time.sleep(1)
        
        # Start the frontend app
        server_manager.start_frontend()
        
        # Wait for shutdown signal
        logger.info("Application running. Press Ctrl+C to shutdown.")
        logger.info("Backend API running on http://localhost:8000")
        logger.info("Frontend UI running on http://localhost:8501")
        shutdown_event.wait()
        
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        # Ensure cleanup happens
        cleanup()
        logger.info("Application shutdown complete")