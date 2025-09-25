# Streamlit Cloud Deployment Guide

## Overview

This document explains how to deploy the SmarTAI application to Streamlit Cloud using the single-file "glue" mode approach.

## Implementation Details

We've created a combined application file `app_cloud.py` that integrates both the FastAPI backend and Streamlit frontend into a single process. This approach is necessary because Streamlit Cloud only supports a single entry point.

### Key Features of the Combined App

1. **Single Process Architecture**: Both frontend and backend run in the same Python process
2. **Threaded Backend**: The FastAPI backend runs in a separate thread to avoid blocking the Streamlit frontend
3. **Shared Dependencies**: All required libraries are declared in `requirements.txt`
4. **Path Management**: Proper Python path configuration to import modules from subdirectories

### How It Works

1. When `app_cloud.py` is executed:
   - The FastAPI backend starts in a background thread on port 8000
   - The Streamlit frontend starts on the main thread (port assigned by Streamlit Cloud)
   - Both components can communicate as needed

2. The application structure:
   - Backend routers are imported and mounted on the FastAPI app
   - Frontend pages are served through Streamlit
   - Shared utilities and models are accessible to both components

## Deployment Instructions

### For Streamlit Cloud

1. Push your code to a GitHub repository
2. In Streamlit Cloud:
   - Set the "App URL" to point to your repository
   - Set the "Main file" to `app_cloud.py`
   - Click "Deploy!"

### For Local Testing

To test the cloud deployment mode locally:

```bash
streamlit run app_cloud.py --client.showSidebarNavigation=False
```

This will start both the backend (on port 8000) and frontend (on a Streamlit-assigned port) in a single process.

## Technical Considerations

### Port Configuration

- Backend runs on port 8000 to avoid conflicts with Streamlit's port
- In a production environment, you may need to adjust firewall settings if external access is required

### Threading Model

- The backend runs in a separate thread to prevent blocking the Streamlit event loop
- Care has been taken to ensure thread-safe operations where needed

### Dependencies

All required dependencies are listed in `requirements.txt`. The combined app requires:

- FastAPI and Uvicorn for the backend
- Streamlit for the frontend
- Langchain and related AI libraries for processing
- Archive processing libraries (rarfile, py7zr) for file handling
- Data visualization libraries (plotly)

## Limitations

1. **Resource Usage**: Running both frontend and backend in the same process uses more memory
2. **Error Isolation**: Errors in one component may affect the other
3. **Scaling**: This approach is suitable for small to medium deployments but may not scale to very high traffic

## Troubleshooting

### Common Issues

1. **Import Errors**: Ensure all dependencies are listed in `requirements.txt`
2. **Port Conflicts**: If port 8001 is in use, modify the port in `app_cloud.py`
3. **Path Issues**: Make sure the project root is in the Python path

### Testing Imports

You can verify that all components import correctly by running:

```bash
python -c "from app_cloud import *; print('All imports successful')"
```

## Future Improvements

1. **Health Checks**: Add endpoints to monitor the health of both frontend and backend
2. **Graceful Shutdown**: Implement proper shutdown procedures for the backend thread
3. **Configuration**: Add environment variables for port configuration and other settings