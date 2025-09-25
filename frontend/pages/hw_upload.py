import streamlit as st
import requests
import os
from PIL import Image
import time
from utils import *

# --- 页面基础设置 ---
# 使用 "wide" 布局以获得更多空间，并设置页面标题和图标
st.set_page_config(
    page_title="上传作业 - 智能作业核查系统", 
    layout="wide",
    page_icon="📂"
)

initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

def render_header():
    """渲染页面头部"""
    col1, col2, col3, _, col4 = st.columns([8,12,18,30,8])
    col = st.columns(1)[0]

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")

    with col2:
        st.page_link("pages/prob_upload.py", label="重新上传作业题目", icon="📤")

    with col3:
        st.page_link("pages/problems.py", label="返回题目识别概览", icon="📖")

    with col4:
        st.page_link("pages/history.py", label="历史记录", icon="🕒")
    
    with col:
        st.markdown("""
    <div class="hero-section">
        <h1 style="text-align: center; color: #000000; margin-bottom: 1rem; font-weight: 700;">🎓 SmarTAI 智能作业评估平台</h1>
        <h4 style='text-align: center; color: #000000;'>高效、智能、全面——您的自动化教学助理。</h4>
    </div>
    """, unsafe_allow_html=True)
        st.markdown("---")
        
render_header()

if 'prob_data' not in st.session_state or not st.session_state.get('prob_data'):
    st.warning("请先在“作业题目上传”页面上传并作业题目文件。")
    st.stop()

# --- 后端服务地址 ---
# BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000/hw_upload")

# --- 初始化会话状态 ---
# if 'processed_data' not in st.session_state:
#     st.session_state.processed_data = None
st.session_state.processed_data = None

# 如果数据已处理，直接跳转，避免重复上传
# if st.session_state.processed_data:
#     st.switch_page("pages/problems.py")

# # --- 页面标题和简介 ---
# st.title("🚀 智能作业核查系统")
# st.markdown("高效、智能、全面——您的自动化教学助理。")
# st.markdown("---")


# --- 1. 作业上传核心功能区 ---
st.markdown('<div class="card">', unsafe_allow_html=True)
st.header("📂 上传学生作业")
st.caption("请将所有学生的作业文件（如 PDF、Word、代码文件、图片等）打包成一个压缩文件后上传。")

uploaded_hw_file = st.file_uploader(
    "拖拽或点击选择作业压缩包",
    type=['zip', 'rar', '7z', 'tar', 'gz', 'bz2'],
    help="支持 .zip, .rar, .7z, .tar.gz 等常见压缩格式。"
)
if uploaded_hw_file is not None:
    st.success(f"文件 '{uploaded_hw_file.name}' 已选择。")
st.markdown('</div>', unsafe_allow_html=True)

# --- 3. 确认与提交区 ---
st.markdown("---")
st.header("✅ 确认并开始核查")
st.info("请检查以上信息。点击下方按钮后，系统将开始处理您的文件。")

# 当用户上传了作业文件后，才激活确认按钮
if uploaded_hw_file is not None:
    if st.button("确认信息，开始智能核查", type="primary", use_container_width=True):
        with st.spinner("正在上传并请求AI分析，请耐心几分钟..."):
            # 准备要发送的文件
            files_to_send = {
                "file": (uploaded_hw_file.name, uploaded_hw_file.getvalue(), uploaded_hw_file.type)
            }
            # (这里可以添加逻辑来处理其他上传的文件，例如答案、测试用例等)
            # st.session_state.task_name=uploaded_hw_file.name
            try:
                # 实际使用时，你需要根据后端API来组织和发送所有数据
                response = requests.post(f"{st.session_state.backend}/hw_preview", files=files_to_send, timeout=600)
                response.raise_for_status()

                # st.session_state.processed_data = response.json()      
                students = response.json()                            
                st.session_state.processed_data = students   #以stu_id为key索引

                # print(st.session_state.processed_data)
          
                st.success("✅ 文件上传成功，后端开始处理！即将跳转至结果预览页面...")
                time.sleep(1) # 短暂显示成功信息
                st.switch_page("pages/stu_preview.py")

            except requests.exceptions.RequestException as e:
                st.error(f"网络或服务器错误: {e}")
            except Exception as e:
                st.error(f"发生未知错误: {e}")
else:
    # 如果用户还未上传文件，则按钮禁用
    st.button("确认信息，开始智能核查", type="primary", use_container_width=True, disabled=True)
    st.warning("请先在上方上传学生作业压缩包。")

inject_pollers_for_active_jobs()