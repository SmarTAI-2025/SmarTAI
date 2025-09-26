"""
SmarTAI Frontend for Streamlit Cloud Deployment
"""
import os
import sys

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import and run the main application
from frontend.main import main

if __name__ == "__main__":
    main()