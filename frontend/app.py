"""
SmarTAI Frontend for Streamlit Cloud Deployment
"""
import os
import sys

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import and run the main application
# Fix the import path - it should be relative to the package structure
from pages.main import main

if __name__ == "__main__":
    main()