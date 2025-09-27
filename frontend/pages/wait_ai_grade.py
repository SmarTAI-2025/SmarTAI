import streamlit as st
import time
import requests
from utils import *
from datetime import datetime

st.set_page_config(
    page_title="正在处理 - 智能作业核查系统",
    layout="wide",
    page_icon="⚙️",
    initial_sidebar_state="collapsed" # 初始折叠有助于减少闪烁
)

initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

# --- 新增：左上角返回主页链接 ---
# 这个链接会固定显示在主内容区域的顶部

# CSS 来彻底隐藏整个侧边-栏容器
#    data-testid="stSidebar" 是整个侧边栏的ID
st.markdown("""
    <style>
        [data-testid="stSidebar"] {
            display: none;
        }
    </style>
    """, unsafe_allow_html=True)

def render_header():
    """渲染页面头部"""
    col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
    col = st.columns(1)[0]

    with col1:
        if st.button("🏠 返回首页"):
            # Reset grading state when returning to main page
            reset_grading_state_on_navigation()
            st.switch_page("pages/main.py")
    
    with col2:
        st.page_link("pages/history.py", label="历史记录", icon="🕒")

    with col3:
        st.page_link("pages/problems.py", label="作业题目", icon="📖")

    with col4:
        st.page_link("pages/stu_preview.py", label="学生作业", icon="📝")
    
    with col5:
        st.page_link("pages/grade_results.py", label="批改结果", icon="📊")

    with col6:
        st.page_link("pages/score_report.py", label="评分报告", icon="💯")

    with col7:
        st.page_link("pages/visualization.py", label="成绩分析", icon="📈")
    
    with col:
        st.markdown("<h1 style='text-align: center; color: #000000;'>🕒 等待AI批改</h1>", 
                   unsafe_allow_html=True)

render_header()

# --- 模拟后端提交和页面跳转 ---

st.title("⚙️ 正在提交作业...")
# st.info("请稍候，AI后台正在进行批改分析...")

# Initialize job status in session state
if 'job_status' not in st.session_state:
    st.session_state.job_status = "pending"

# 2. 【核心逻辑】检查是否存在从其他页面传来的“触发标志”
if st.session_state.get('trigger_ai_grading'):
    
    # 3. 【至关重要】立刻“消费”掉这个标志，防止刷新页面时重复执行！
    del st.session_state.trigger_ai_grading
    
    # 4. 现在，在这里安全地执行你那段只需要运行一次的代码
    st.info("已接收到任务请求，请稍候，正在提交至AI后台正在进行批改分析...")
    try:
        # 使用 with st.spinner 来提供更好的用户反馈
        with st.spinner('正在提交批改任务，请稍候...'):
            # Use the batch grading endpoint to grade all students
            result = requests.post(
                f"{st.session_state.backend}/ai_grading/grade_all/",
                json={},
                timeout=600
            )
            result.raise_for_status()
            job_response = result.json()
            job_id = job_response.get("job_id")
        
        if not job_id:
            st.error("后端未返回 job_id")
        else:
            # 2. 从 session_state 中获取任务名，如果不存在则提供一个默认名
            task_name = st.session_state.get("task_name", "未命名任务")
            # Only delete if it exists
            if "task_name" in st.session_state:
                del st.session_state.task_name
            
            # 3. 获取并格式化当前提交时间
            submission_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 4. 创建一个包含所有任务信息的字典
            task_details = {
                "name": task_name,
                "submitted_at": submission_time
            }

            # 5. 将这个任务的详细信息存入全局的任务字典中，以 job_id 作为唯一的键
            if "jobs" not in st.session_state:
                st.session_state.jobs = {} # Ensure it exists
            
            # Add the new job
            st.session_state.jobs[job_id] = task_details
            # Also store the job_id for immediate access
            st.session_state.current_job_id = job_id
            # Store job_id for status checking
            st.session_state.checking_job_id = job_id
            
            # Debug information
            st.write(f"Stored job ID: {job_id}")
            st.write(f"Jobs in session state: {list(st.session_state.jobs.keys())}")
            
            # 6. 更新成功提示信息，显示用户友好的任务名
            _, img_col, _ = st.columns([1, 1, 1])
            with img_col:
                st.image(
                    "static/checkmark.svg",
                    caption=f"批改任务：{task_name}已成功提交至AI后台处理！",
                    width=200
                )
            
            # 使用 st.rerun() 立即刷新页面。
            # 刷新后，因为标志已被删除，所以上面的代码不会再次运行。
            # 同时，下面的轮询器注入代码会检测到新的 job_id 并开始轮询。
            st.rerun()
            
    except Exception as e:
        st.error(f"提交失败：{e}")

# If we have a job to check status for
if 'checking_job_id' in st.session_state:
    job_id = st.session_state.checking_job_id
    
    # Display current status
    st.subheader("任务状态")
    status_container = st.empty()
    
    # Add refresh button
    if st.button("🔄 刷新状态"):
        try:
            response = requests.get(
                f"{st.session_state.backend}/ai_grading/grade_result/{job_id}",
                timeout=10
            )
            if response.status_code == 200:
                result = response.json()
                st.session_state.job_status = result.get("status", "unknown")
            else:
                st.error(f"获取状态失败: {response.status_code}")
        except Exception as e:
            st.error(f"获取状态时出错: {e}")
    
    # Auto-check status
    try:
        response = requests.get(
            f"{st.session_state.backend}/ai_grading/grade_result/{job_id}",
            timeout=10
        )
        if response.status_code == 200:
            result = response.json()
            status = result.get("status", "unknown")
            st.session_state.job_status = status
            
            # Update display based on status
            if status == "pending":
                status_container.info("🕒 任务正在处理中，请稍候...")
            elif status == "completed":
                status_container.success("✅ 任务已完成！正在跳转到批改结果页面...")
                # Remove the job from checking
                if 'checking_job_id' in st.session_state:
                    del st.session_state.checking_job_id
                # Set the current job as selected
                st.session_state.selected_job_id = job_id
                # Set newly submitted job ID
                st.session_state.newly_submitted_job_id = job_id
                # Wait a moment and then redirect
                time.sleep(2)
                st.switch_page("pages/grade_results.py")
            elif status == "error":
                status_container.error(f"❌ 任务处理出错: {result.get('message', '未知错误')}")
                # Remove the job from checking
                if 'checking_job_id' in st.session_state:
                    del st.session_state.checking_job_id
            else:
                status_container.warning(f"⚠️ 当前状态: {status}")
        else:
            status_container.error(f"获取状态失败: {response.status_code}")
    except Exception as e:
        status_container.error(f"获取状态时出错: {e}")

    # Show job details
    if job_id in st.session_state.jobs:
        task_details = st.session_state.jobs[job_id]
        st.write(f"任务名称: {task_details.get('name', '未命名任务')}")
        st.write(f"提交时间: {task_details.get('submitted_at', '未知时间')}")

# Auto-refresh every 5 seconds if we're still checking
if 'checking_job_id' in st.session_state:
    st.markdown(
        """
        <script>
        setTimeout(function(){
            window.parent.location.reload();
        }, 5000);
        </script>
        """,
        unsafe_allow_html=True
    )
    st.info("页面将在5秒后自动刷新以检查任务状态...")

inject_pollers_for_active_jobs()

def reset_grading_state_on_navigation():
    """Reset grading state when navigating away from grading pages"""
    try:
        # Reset backend grading state
        response = requests.delete(
            f"{st.session_state.backend}/ai_grading/reset_all_grading",
            timeout=5
        )
        if response.status_code == 200:
            print("Backend grading state reset successfully on navigation")
        else:
            print(f"Failed to reset backend grading state on navigation: {response.status_code}")
    except Exception as e:
        print(f"Error resetting backend grading state on navigation: {e}")
    
    # Clear frontend grading-related session state
    keys_to_clear = [
        'ai_grading_data',
        'sample_data',
        'selected_job_id',
        'report_job_selector',
        'checking_job_id',
        'job_status'
    ]
    
    for key in keys_to_clear:
        if key in st.session_state:
            del st.session_state[key]