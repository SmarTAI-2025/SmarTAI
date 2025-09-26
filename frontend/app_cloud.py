"""
SmarTAI Frontend Cloud Startup Script
This script is specifically designed for cloud deployments like Streamlit Community Cloud
"""
import os
import sys
from utils import UTILS_BACKEND_URL

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set the backend URL from environment variables or use default
backend_url = os.environ.get("BACKEND_URL", UTILS_BACKEND_URL)

def main():
    # Import and run the main application
    from pages.main import render_st
    
    # Run the Streamlit app with proper configuration
    render_st(backend_url)

if __name__ == "__main__":
    main()