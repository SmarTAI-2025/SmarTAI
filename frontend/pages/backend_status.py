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
    page_title="后端状态 - SmarTAI",
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
                "message": "后端运行正常且健康",
                "details": health_data
            }
        else:
            return {
                "status": "error",
                "message": f"后端返回状态码 {health_response.status_code}",
                "details": {}
            }
    except requests.exceptions.ConnectionError:
        return {
            "status": "disconnected",
            "message": "无法连接到后端。请检查后端服务是否正在运行。",
            "details": {}
        }
    except requests.exceptions.Timeout:
        return {
            "status": "timeout",
            "message": "后端请求超时。后端可能运行缓慢或无响应。",
            "details": {}
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"检查后端状态时出错: {str(e)}",
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

    status_display_names = {
        "connected": "已连接",
        "disconnected": "已断开",
        "timeout": "超时",
        "error": "错误"
    }
    
    color = status_colors.get(status_info["status"], "#6B7280")  # gray as default
    icon = status_icons.get(status_info["status"], "❓")
    display_status = status_display_names.get(status_info["status"], status_info["status"].title())
    
    st.markdown(f"""
    <div style="background: white; padding: 1.5rem; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-left: 6px solid {color}; margin-bottom: 1rem;">
        <div style="display: flex; align-items: center; margin-bottom: 1rem;">
            <span style="font-size: 2rem; margin-right: 1rem;">{icon}</span>
            <div>
                <h3 style="margin: 0; color: {color};">{status_info["message"]}</h3>
                <p style="margin: 0; color: #6B7280;">状态: {display_status}</p>
            </div>
        </div>
        {f'<div style="background: #F9FAFB; padding: 1rem; border-radius: 8px; margin-top: 1rem;"><pre style="margin: 0; white-space: pre-wrap;">{json.dumps(status_info["details"], indent=2, ensure_ascii=False)}</pre></div>' if status_info["details"] else ''}
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
            <h1>🔍 后端连接状态</h1>
            <p>检查前端和后端服务之间的连接状态</p>
        </div>
        """, unsafe_allow_html=True)
    
    # Get backend URL from session state
    backend_url = st.session_state.get("backend", "http://localhost:8000")
    
    st.markdown(f"""
    <div style="background: #F0F9FF; padding: 1rem; border-radius: 8px; margin-bottom: 2rem;">
        <h4>后端 URL 配置</h4>
        <p><strong>当前后端 URL:</strong> <code>{backend_url}</code></p>
        <p><em>此 URL 通过 BACKEND_URL 环境变量设置。</em></p>
    </div>
    """, unsafe_allow_html=True)
    
    # Check backend status
    with st.spinner("正在检查后端状态..."):
        status_info = check_backend_status(backend_url)
    
    # Display status
    render_status_card(status_info)
    
    # Show additional information
    st.markdown("### 📋 连接详情")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 🧪 测试端点")
        if status_info["status"] == "connected":
            try:
                # Test docs endpoint
                docs_response = requests.get(f"{backend_url}/docs", timeout=5)
                if docs_response.status_code == 200:
                    st.success("✅ API 文档可访问")
                else:
                    st.warning(f"⚠️ API 文档返回状态码 {docs_response.status_code}")
            except:
                st.error("❌ 无法访问 API 文档")
        else:
            st.info("📡 等待后端连接测试")
    
    with col2:
        st.markdown("#### ⚙️ 配置检查")
        if "smartai" in backend_url.lower():
            st.success("✅ 后端 URL 似乎已为 Render 部署正确配置")
        elif "localhost" in backend_url:
            st.info("ℹ️ 后端已配置为本地开发模式")
        else:
            st.warning("⚠️ 后端 URL 格式不常见")
    
    # Auto-refresh option
    st.markdown("---")
    if st.button("🔄 刷新状态"):
        st.rerun()
    
    # Help information
    st.markdown("### ℹ️ 帮助")
    st.markdown("""
    **如果您遇到连接问题：**
    1. 检查后端服务是否正在运行
    2. 确认 BACKEND_URL 环境变量已正确设置
    3. 确保后端的 FRONTEND_URLS 环境变量包含了您的前端 URL
    4. 检查是否存在防火墙或网络限制
    
    **对于 Render 部署：**
    - 后端 URL 格式应为： `https://your-app-name.onrender.com`
    - 前端 URL 应被添加到 Render 上的 FRONTEND_URLS 环境变量中
    """)

if __name__ == "__main__":
    main()