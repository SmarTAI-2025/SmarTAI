"""
Backend Status Page
This page shows the connection status between the frontend and backend.
"""
import streamlit as st
import requests
import time
import os
import sys
import json

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import from utils.py (the file, not the folder)
from utils import load_custom_css, initialize_session_state

# 页面配置
st.set_page_config(
    page_title="Backend Status - SmarTAI",
    page_icon="🔍",
    layout="wide"
)

def check_backend_status(backend_url):
    """Check the backend status by calling the health endpoint"""
    try:
        # Check health endpoint
        health_response = requests.get(f"{backend_url}/health", timeout=5)
        if health_response.status_code == 200:
            health_data = health_response.json()
            return {
                "status": "connected",
                "message": "Backend is running and healthy",
                "details": health_data
            }
        else:
            return {
                "status": "error",
                "message": f"Backend returned status code {health_response.status_code}",
                "details": {}
            }
    except requests.exceptions.ConnectionError:
        return {
            "status": "disconnected",
            "message": "Cannot connect to backend. Please check if the backend is running.",
            "details": {}
        }
    except requests.exceptions.Timeout:
        return {
            "status": "timeout",
            "message": "Request to backend timed out. The backend might be slow or unresponsive.",
            "details": {}
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error checking backend status: {str(e)}",
            "details": {}
        }

def render_status_card(status_info):
    """Render a status card with appropriate styling"""
    status_colors = {
        "connected": "#10B981",      # green
        "disconnected": "#EF4444",   # red
        "timeout": "#F59E0B",        # amber
        "error": "#EF4444"           # red
    }
    
    status_icons = {
        "connected": "✅",
        "disconnected": "❌",
        "timeout": "⏰",
        "error": "⚠️"
    }
    
    color = status_colors.get(status_info["status"], "#6B7280")  # gray as default
    icon = status_icons.get(status_info["status"], "❓")
    
    st.markdown(f"""
    <div style="background: white; padding: 1.5rem; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-left: 6px solid {color}; margin-bottom: 1rem;">
        <div style="display: flex; align-items: center; margin-bottom: 1rem;">
            <span style="font-size: 2rem; margin-right: 1rem;">{icon}</span>
            <div>
                <h3 style="margin: 0; color: {color};">{status_info["message"]}</h3>
                <p style="margin: 0; color: #6B7280;">Status: {status_info["status"].title()}</p>
            </div>
        </div>
        {f'<div style="background: #F9FAFB; padding: 1rem; border-radius: 8px; margin-top: 1rem;"><pre style="margin: 0; white-space: pre-wrap;">{json.dumps(status_info["details"], indent=2)}</pre></div>' if status_info["details"] else ''}
    </div>
    """, unsafe_allow_html=True)

def main():
    """Main function for the backend status page"""
    # 加载CSS和初始化
    load_custom_css()
    initialize_session_state()
    
    # Add return to home button
    col1, col2 = st.columns([12, 56])

    with col1:
        st.page_link("pages/main.py", label="返回首页", icon="🏠")

    with col2:
        st.markdown("""
        <div style="text-align: center; margin-bottom: 2rem;">
            <h1>🔍 Backend Connection Status</h1>
            <p>Check the connection status between the frontend and backend services</p>
        </div>
        """, unsafe_allow_html=True)
    
    # Get backend URL from session state
    backend_url = st.session_state.get("backend", "http://localhost:8000")
    
    st.markdown(f"""
    <div style="background: #F0F9FF; padding: 1rem; border-radius: 8px; margin-bottom: 2rem;">
        <h4>Backend URL Configuration</h4>
        <p><strong>Current Backend URL:</strong> <code>{backend_url}</code></p>
        <p><em>This URL is set through the BACKEND_URL environment variable.</em></p>
    </div>
    """, unsafe_allow_html=True)
    
    # Check backend status
    with st.spinner("Checking backend status..."):
        status_info = check_backend_status(backend_url)
    
    # Display status
    render_status_card(status_info)
    
    # Show additional information
    st.markdown("### 📋 Connection Details")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 🧪 Test Endpoints")
        if status_info["status"] == "connected":
            try:
                # Test docs endpoint
                docs_response = requests.get(f"{backend_url}/docs", timeout=5)
                if docs_response.status_code == 200:
                    st.success("✅ API Documentation is accessible")
                else:
                    st.warning(f"⚠️ API Documentation returned status {docs_response.status_code}")
            except:
                st.error("❌ Cannot access API Documentation")
        else:
            st.info("📡 Backend connection test pending")
    
    with col2:
        st.markdown("#### ⚙️ Configuration Check")
        if "smartai-backend" in backend_url.lower():
            st.success("✅ Backend URL appears to be correctly configured for Render deployment")
        elif "localhost" in backend_url:
            st.info("ℹ️ Backend is configured for local development")
        else:
            st.warning("⚠️ Backend URL format is unusual")
    
    # Auto-refresh option
    st.markdown("---")
    if st.button("🔄 Refresh Status"):
        st.rerun()
    
    # Help information
    st.markdown("### ℹ️ Help")
    st.markdown("""
    **If you're experiencing connection issues:**
    1. Check that the backend service is running
    2. Verify the BACKEND_URL environment variable is correctly set
    3. Ensure the FRONTEND_URLS environment variable on the backend includes your frontend URL
    4. Check that there are no firewall or network restrictions
    
    **For Render deployment:**
    - The backend URL should be in the format: `https://your-app-name.onrender.com`
    - The frontend URL should be added to the FRONTEND_URLS environment variable on Render
    """)

if __name__ == "__main__":
    main()