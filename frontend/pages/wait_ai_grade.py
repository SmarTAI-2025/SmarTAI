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
    col1, _, col2 = st.columns([8,50,8])

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")
    
    with col2:
        st.page_link("pages/history.py", label="历史记录", icon="🕒")
        
render_header()

# --- 模拟后端提交和页面跳转 ---

st.title("⚙️ 正在提交作业...")
# st.info("请稍候，AI后台正在进行批改分析...")

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
            
            # Debug information
            st.write(f"Stored job ID: {job_id}")
            st.write(f"Jobs in session state: {list(st.session_state.jobs.keys())}")
            
            # 6. 更新成功提示信息，显示用户友好的任务名
            _, img_col, _ = st.columns([1, 1, 1])
            with img_col:
                st.image(
                    "frontend/static/checkmark.svg",
                    caption=f"批改任务：{task_name}已成功提交至AI后台处理！",
                    width=200
                )
            
            # 使用 st.rerun() 立即刷新页面。
            # 刷新后，因为标志已被删除，所以上面的代码不会再次运行。
            # 同时，下面的轮询器注入代码会检测到新的 job_id 并开始轮询。
            st.rerun()
            
    except Exception as e:
        st.error(f"提交失败：{e}")


# # 5. 页面的其余部分，比如显示标题和当前任务列表
# st.title("任务执行与轮询")
# st.write("这里会显示所有正在进行的任务。当任务完成时，你会收到弹窗提醒。")

# if st.session_state.jobs:
#     st.write("当前会话中的活动任务：")
#     for j in st.session_state.jobs:
#         st.info(f"- {j}")
# else:
#     st.write("当前没有正在执行的任务。")


# # 6. 在脚本末尾注入轮询器（和之前一样）
# pollers_html = get_global_pollers_html()
# if pollers_html:
#     with st.sidebar:
#         components.html(pollers_html, height=0)






# # 模拟：我们在 session_state 中记录一个任务的开始时间，代表任务已启动
# st.session_state['active_job_start_time'] = time.time()
# # 清理旧的完成状态，以防万一
# if 'job_completed' in st.session_state:
#     del st.session_state['job_completed']


# 使用 st.spinner 来提供视觉反馈
# with st.spinner('任务已提交至后台，本页面稍后将自动跳转到历史批改记录。\n 当任务完成时，你会收到弹窗提醒。'):
st.success('任务已提交至后台，本页面将于5秒后将自动跳转到历史批改记录。\n 当任务完成时，你会收到弹窗提醒。')
time.sleep(3) # 后续对接后端

# 4. 跳转回历史批改记录界面
st.switch_page("pages/grade_results.py")

inject_pollers_for_active_jobs()