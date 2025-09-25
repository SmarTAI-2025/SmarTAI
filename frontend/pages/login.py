import streamlit as st
import time

# 设置页面配置
st.set_page_config(
    page_title="SmarTAI作业平台",
    page_icon="🎓",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# 自定义CSS样式
st.markdown("""
<style>
    /* 导入Google Fonts */
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;600;700&display=swap');
    
    /* 隐藏Streamlit默认元素 */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* 全局样式 */
    * {
        font-family: 'Noto Sans SC', 'Helvetica Neue', Helvetica, Arial, sans-serif;
    }
    
    /* 主容器样式 */
    .main {
        padding-top: 2rem;
    }
    
    /* 标题样式 - 使用项目主色调 */
    .title {
        text-align: center;
        font-size: 3rem;
        font-weight: 700;
        margin-bottom: 0.5rem;
        color: #1E3A8A;
        text-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    }
    
    /* 副标题样式 */
    .subtitle {
        text-align: center;
        color: #64748B;
        font-size: 1.2rem;
        margin-bottom: 3rem;
        font-weight: 400;
    }
    
    /* 登录卡片容器 - 使用项目统一风格 */
    .login-container {
        background: white;
        padding: 3rem 2rem;
        border-radius: 15px;
        box-shadow: 0 8px 25px rgba(0,0,0,0.1);
        border: 1px solid #EAEAEA;
        max-width: 400px;
        margin: 0 auto;
        border-top: 4px solid #1E3A8A;
    }
    
    /* 输入框样式 - 使用项目统一风格 */
    .stTextInput > div > div > input {
        border-radius: 8px !important;
        border: 2px solid #F1F5F9 !important;
        padding: 1rem 1.2rem !important;
        font-size: 1rem !important;
        transition: all 0.3s ease !important;
        background: white !important;
        color: #334155 !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1) !important;
    }
    
    .stTextInput > div > div > input:focus {
        border-color: #1E3A8A !important;
        box-shadow: 0 0 0 3px rgba(30, 58, 138, 0.1), 0 1px 3px rgba(0, 0, 0, 0.1) !important;
        background: white !important;
        color: #334155 !important;
        outline: none !important;
    }
    
    /* 移除错误状态的红色边框 */
    .stTextInput > div > div > input:invalid,
    .stTextInput > div > div > input[aria-invalid="true"] {
        border-color: #F1F5F9 !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1) !important;
    }
    
    /* 输入框容器样式 */
    .stTextInput > div {
        background: transparent !important;
        border: none !important;
    }
    
    .stTextInput > div > div {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }
    
    /* 输入框占位符文字样式 */
    .stTextInput > div > div > input::placeholder {
        color: #64748B !important;
        opacity: 1 !important;
    }
    
    /* 密码框眼睛图标样式调整 */
    .stTextInput[data-baseweb="input"] button {
        background: transparent !important;
        border: none !important;
        right: 10px !important;
        top: 50% !important;
        transform: translateY(-50%) !important;
    }
    
    /* 密码输入框特殊样式 */
    .stTextInput input[type="password"] {
        padding-right: 3rem !important;
    }
    
    /* 按钮样式 - 使用项目统一风格 */
    .stButton > button {
        width: 100%;
        background: #1E3A8A;
        color: white;
        border: none;
        border-radius: 8px;
        padding: 1rem 2rem;
        font-size: 1rem;
        font-weight: 600;
        transition: all 0.3s ease;
        margin-top: 1rem;
        box-shadow: 0 4px 15px rgba(30, 58, 138, 0.2);
        min-height: 50px;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    
    .stButton > button:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 25px rgba(30, 58, 138, 0.3);
        background: #3B82F6;
    }
    
    /* 标签样式 */
    .stTextInput > label {
        color: #334155 !important;
        font-weight: 600;
        font-size: 1rem;
        margin-bottom: 0.5rem;
    }
    
    /* 成功/错误消息样式 - 使用项目统一风格 */
    .success-msg {
        background: #10B981;
        color: white;
        padding: 1rem;
        border-radius: 8px;
        text-align: center;
        margin: 1rem 0;
        font-weight: 600;
    }
    
    .error-msg {
        background: #EF4444;
        color: white;
        padding: 1rem;
        border-radius: 8px;
        text-align: center;
        margin: 1rem 0;
        font-weight: 600;
    }
    
    /* 背景样式 - 使用项目统一风格 */
    .stApp {
        background-color: #F8FAFC;
        min-height: 100vh;
    }
    
    /* 图标样式 - 使用项目统一风格 */
    .icon {
        font-size: 3rem;
        text-align: center;
        color: #1E3A8A;
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
    }
    
    .icon-container {
        background: white;
        border-radius: 50%;
        width: 80px;
        height: 80px;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto 1.5rem auto;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        border: 2px solid #1E3A8A;
    }
    
    /* 模态框样式 - 使用项目统一风格 */
    .modal-overlay {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 999;
    }
    
    .modal-content {
        background: white;
        border-radius: 15px;
        padding: 3rem 2.5rem;
        width: 90%;
        max-width: 450px;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
        position: relative;
        border: 1px solid #EAEAEA;
        border-top: 4px solid #1E3A8A;
    }
    
    .modal-close {
        position: absolute;
        top: 1.5rem;
        right: 1.5rem;
        background: #F1F5F9;
        border: 1px solid #EAEAEA;
        font-size: 1.2rem;
        cursor: pointer;
        color: #64748B;
        width: 35px;
        height: 35px;
        display: flex;
        align-items: center;
        justify-content: center;
        border-radius: 8px;
        transition: all 0.3s ease;
        font-weight: 600;
    }
    
    .modal-close:hover {
        background: #EF4444;
        color: white;
        border-color: #EF4444;
    }
    
    .modal-title {
        text-align: center;
        color: #1E3A8A;
        margin-bottom: 2rem;
        font-size: 1.8rem;
        font-weight: 700;
    }
    
    /* 模态框内的输入框样式 */
    .modal-content .stTextInput > div > div > input {
        background: white;
        border: 2px solid #F1F5F9;
        color: #334155;
        border-radius: 8px;
        padding: 1rem 1.2rem;
        font-size: 1rem;
        transition: all 0.3s ease;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    }
    
    .modal-content .stTextInput > div > div > input:focus {
        border-color: #1E3A8A;
        background: white;
        box-shadow: 
            0 1px 3px rgba(0, 0, 0, 0.1),
            0 0 0 3px rgba(30, 58, 138, 0.1);
    }
    
    .modal-content .stTextInput > div > div > input::placeholder {
        color: #64748B;
    }
    
    .modal-content .stTextInput > label {
        color: #334155;
        font-weight: 600;
        font-size: 1rem;
        margin-bottom: 0.5rem;
    }
    
    /* 模态框按钮样式 */
    .modal-content .stButton > button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
        border: none;
        min-height: 50px;
    }
    
    .modal-content .stButton > button:hover {
        transform: translateY(-2px);
    }
    
    /* 主登录按钮样式 - 使用项目统一风格 */
    .main-login-button {
        background: #1E3A8A;
        color: white;
        border: none;
        border-radius: 8px;
        padding: 1rem 3rem;
        font-size: 1.2rem;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(30, 58, 138, 0.2);
        margin: 2rem auto;
        display: block;
    }
    
    .main-login-button:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 25px rgba(30, 58, 138, 0.3);
        background: #3B82F6;
    }
</style>
""", unsafe_allow_html=True)

# 初始化session state
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'username' not in st.session_state:
    st.session_state.username = ""
if 'show_login_modal' not in st.session_state:
    st.session_state.show_login_modal = False

# 登录函数
def login(username, password):
    # 这里可以添加真实的认证逻辑
    # 目前使用简单的演示逻辑
    if username == "admin" and password == "123456":
        return True
    elif username and password:  # 任何非空用户名和密码都可以登录（演示用）
        return True
    return False

# 主应用
def main():
    # 如果已登录，直接跳转到主界面
    if st.session_state.logged_in:
        st.switch_page("main.py")
        return
    
    # 如果要显示登录表单
    if st.session_state.show_login_modal:
        render_login_page()
        return
    
    # 默认显示欢迎页面
    render_home_page()

def render_home_page():
    """渲染主页面"""
    st.markdown('<div class="title" data-text="SmarTAI">SmarTAI</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">智能作业评估平台</div>', unsafe_allow_html=True)

    # 主界面内容
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        # 图标
        st.markdown('''
        <div class="icon-container">
            <div class="icon">🎓</div>
        </div>
        ''', unsafe_allow_html=True)
        
        # 主登录按钮
        st.markdown('<div style="text-align: center; margin: 2rem 0;">', unsafe_allow_html=True)
        if st.button("🚀 开始学习", key="main_login_btn", help="点击开始您的学习之旅", use_container_width=True):
            st.session_state.show_login_modal = True
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        
        # 平台介绍
        st.markdown("""
        <div style="text-align: center; margin-top: 3rem; color: #334155; background: white; padding: 2rem; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); border: 1px solid #EAEAEA; border-top: 4px solid #1E3A8A;">
            <h3 style="color: #1E3A8A; margin-bottom: 1rem; font-weight: 700;">🌟 平台特色</h3>
            <p style="margin: 0.8rem 0; font-size: 1rem; color: #64748B;">🤖 <strong>AI智能批改</strong> - 快速准确的作业评估</p>
            <p style="margin: 0.8rem 0; font-size: 1rem; color: #64748B;">📊 <strong>详细反馈</strong> - 个性化学习建议</p>
            <p style="margin: 0.8rem 0; font-size: 1rem; color: #64748B;">📈 <strong>学习跟踪</strong> - 实时进度监控</p>
        </div>
        """, unsafe_allow_html=True)

def render_welcome_page():
    """渲染欢迎页面（登录后）"""
    st.markdown('<div class="title">🎉 欢迎使用 SmarTAI 智能作业评估平台</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">登录成功！</div>', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown('<div class="success-msg">🚀 您已成功登录系统！</div>', unsafe_allow_html=True)
        st.markdown(f"<p style='text-align: center; font-size: 1.2rem; color: white;'>欢迎，<strong>{st.session_state.username}</strong>！</p>", unsafe_allow_html=True)
        
        if st.button("退出登录", key="logout"):
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.session_state.show_login_modal = False
            st.rerun()

def render_login_page():
    """渲染独立的登录页面"""
    
    # 登录页面标题
    st.markdown('<div class="title">🔐 用户登录</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">请输入您的账户信息进入系统</div>', unsafe_allow_html=True)
    
    # 创建居中的登录表单
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        # 登录图标
        st.markdown('''
        <div class="icon-container" style="margin: 2rem auto;">
            <div class="icon">👤</div>
        </div>
        ''', unsafe_allow_html=True)
        
        # 欢迎信息
        st.markdown("""
        <div style="text-align: center; margin: 2rem 0;">
            <h3 style="color: #1E3A8A; margin: 0; font-size: 1.5rem; font-weight: 700;">欢迎回来！</h3>
            <p style="color: #64748B; margin: 0.5rem 0;">请输入您的登录凭据</p>
        </div>
        """, unsafe_allow_html=True)
        
        # 登录表单 - 直接使用，不包装在额外容器中
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input(
                "👤 用户名",
                placeholder="请输入您的用户名",
                key="login_username",
                help="输入您的用户名或邮箱地址"
            )
            
            password = st.text_input(
                "🔒 密码",
                type="password", 
                placeholder="请输入您的密码",
                key="login_password",
                help="输入您的登录密码"
            )
            
            st.markdown('<br>', unsafe_allow_html=True)
            
            # 按钮区域
            col_login, col_cancel = st.columns([3, 1])
            
            with col_login:
                login_btn = st.form_submit_button(
                    "🚀 立即登录", 
                    use_container_width=True,
                    help="点击登录进入系统"
                )
            
            with col_cancel:
                cancel_btn = st.form_submit_button(
                    "返回", 
                    use_container_width=True,
                    help="返回主页"
                )
            
            # 处理登录逻辑
            if login_btn:
                if not username or not password:
                    st.error("❌ 请输入完整的用户名和密码")
                elif login(username, password):
                    st.session_state.logged_in = True
                    st.session_state.username = username
                    st.session_state.show_login_modal = False
                    st.success("✅ 登录成功！正在进入系统...")
                    time.sleep(1)
                    # 跳转到主界面
                    st.switch_page("main.py")
                else:
                    st.error("❌ 用户名或密码错误，请检查后重试")
            
            if cancel_btn:
                st.session_state.show_login_modal = False
                st.rerun()
        
        st.markdown('<br>', unsafe_allow_html=True)
        
        # 演示账户信息
        st.markdown("""
        <div style="background: #F0F7FF; border: 1px solid #1E3A8A; border-radius: 8px; padding: 1rem; margin: 1rem 0; color: #1E3A8A; font-weight: 600;">
            💡 <strong>演示账户</strong>: admin / 123456
        </div>
        """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
