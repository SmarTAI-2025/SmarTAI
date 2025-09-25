# pages/stu_preview.py

import streamlit as st
import pandas as pd
from utils import *
import re

# --- 页面基础设置 (建议添加) ---
st.set_page_config(
    page_title="学生作业总览 - 智能作业核查系统",
    layout="wide",
    page_icon="📖",
    initial_sidebar_state="expanded"  # 保留Student info侧边栏展开
)

initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

def render_header():
    """渲染页面头部"""
    col1, col2, col3, col4, _, col5 = st.columns([8,13,13,13,15,8])
    col = st.columns(1)[0]

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")

    with col2:
        st.page_link("pages/prob_upload.py", label="重新上传作业题目", icon="📤")

    with col3:
        st.page_link("pages/problems.py", label="返回题目识别概览", icon="📖")

    with col4:
        st.page_link("pages/hw_upload.py", label="重新上传学生作答", icon="📤")

    with col5:
        st.page_link("pages/history.py", label="历史记录", icon="🕒")
    
    with col:
        st.markdown("""
    <div class="hero-section">
        <h1 style="text-align: center; color: #000000; margin-bottom: 1rem; font-weight: 700;">📝 学生作业作答总览</h1>
        <p style='text-align: center; color: #000000;'>您可以拖拽表格右下角以调整表格大小</p>
    </div>
    """, unsafe_allow_html=True)
        st.markdown("---")
        
render_header()

# --- 安全检查 ---
# 检查必要的数据是否已加载
if 'prob_data' not in st.session_state or not st.session_state.get('prob_data'):
    st.warning("请先在“作业题目上传”页面上传并处理作业题目文件。")
    # st.page_link("pages/prob_upload.py", label="返回题目上传页面", icon="📤")
    st.stop()
if 'processed_data' not in st.session_state or not st.session_state.get('processed_data'):
    st.warning("请先在“学生作业上传”页面上传并处理学生作答文件。")
    # st.page_link("pages/hw_upload.py", label="返回作答上传页面", icon="📤")
    st.stop()


# --- 侧边栏导航 ---
with st.sidebar:
    st.header("导航")
    
    # 链接到其他主要功能页面
    # st.page_link("pages/problems.py", label="题目识别概览", icon="📝") # 假设题目识别页面文件名
    
    # 当前页面的链接，点击它相当于刷新到总览状态
    st.page_link("pages/stu_preview.py", label="学生作答总览", icon="📝")

    # --- 学生列表导航 ---
    # 点击每个学生的名字，会通过 session_state 传递ID并切换到详情页面
    with st.expander("按学生查看", expanded=True):
        student_list = sorted(list(st.session_state.processed_data.keys()))

        if not student_list:
            st.caption("暂无学生数据")
        else:
            # 定义一个回调函数，用于设置选中的学生ID并切换页面
            def select_student(sid):
                st.session_state['selected_student_id'] = sid
                # st.switch_page("pages/stu_details.py")

            for sid in student_list:
                # 这里我们仍然使用 button，因为它需要触发一个带参数的回调
                # 并执行 st.switch_page() 这个动作，这是 st.page_link 做不到的。
                # 但通过 width='stretch' 可以让它样式上更统一。
                if st.button(
                    sid, 
                    key=f"btn_student_{sid}", 
                    on_click=select_student,
                    args=(sid,),
                    use_container_width=True
                ):
                    st.session_state['selected_student_id'] = sid
                    st.switch_page("pages/stu_details.py")


# --- 主页面内容：学生总览仪表盘 ---

def render_students_dashboard():
    """
    显示一个包含所有学生作业状态的总览表
    """
    # st.header("📖 学生作业总览")
    
    students_data = st.session_state.processed_data
    problems_data = st.session_state.prob_data
    
    if not students_data or not problems_data:
        st.info("没有足够的学生或题目信息来生成总览。")
        return

    # 准备用于DataFrame的数据
    dashboard_data = []
    
    # --- 修改1：按学号递增排序 ---
    # 在循环前，先获取排序后的学号列表
    sorted_stu_ids = sorted(students_data.keys())

    for stu_id in sorted_stu_ids:
        student_data = students_data[stu_id]
        name = student_data.get("stu_name", "未知姓名")
        row = {
            '学号': stu_id,
            '姓名': name,
            }

        answers = student_data.get('stu_ans', [])
        ans_qid_list = []
        for ans in answers:
            q_id = ans.get('q_id')
            ans_qid_list.append(q_id)
            num = ans.get('number', '未知题号')
            q_num = "题目 "+str(num)
            
            if ans.get('flag'):
                row[q_num] = "🚩 需人工处理"
            elif not ans.get('content'):
                row[q_num] = "❌ 未提交" 
            else:
                row[q_num] = "✅ 已提交并识别成功"

        for q_id in problems_data.keys():
            if q_id not in ans_qid_list:
                q_num = problems_data[q_id].get('number', '未知题号')
                row[q_num] = "❌ 未提交"

        dashboard_data.append(row)
        
    if dashboard_data:
        df = pd.DataFrame(dashboard_data)

        # --- 修改2：按题号递增排序 ---
        # 定义一个函数来实现“自然排序”，确保“题2”在“题10”之前
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', str(s))]

        # 获取所有题目列并进行自然排序
        problem_columns = [col for col in df.columns if col not in ['学号', '姓名']]
        sorted_problem_columns = sorted(problem_columns, key=natural_sort_key)
        
        # 定义最终的列顺序
        final_column_order = ['学号', '姓名'] + sorted_problem_columns
        
        # 应用列顺序
        df = df[final_column_order]

        # --- 修改3：同时固定学号和姓名列 ---
        df = df.set_index(['学号', '姓名'])
        
        st.dataframe(df, use_container_width=True)
    else:
        st.info("无法生成学生作业总览。")

# 渲染总览视图
render_students_dashboard()

# --- 新增：右下角跳转链接 ---
def start_ai_grading_and_navigate():
    """
    这个函数做了两件事：
    1. 在 session_state 中设置一个“一次性触发”的标志。
    2. 命令 Streamlit 跳转到任务轮询页面。
    """
    st.session_state.trigger_ai_grading = True  # 使用与目标页面匹配的标志
    # st.switch_page("pages/wait_ai_grade.py")   # 跳转到你的目标页面

# ----------------------------------------------------
# 添加一个分隔符，使其与主内容分开
st.divider()

# 使用列布局将按钮推到右侧 (这部分和你的代码一样)
col_spacer, col_button = st.columns([48, 8])

with col_button:
    # 2. 创建一个按钮，并告诉它在被点击时调用上面的函数
    if st.button(
        "🚀 开启AI批改", 
        on_click=start_ai_grading_and_navigate, 
        use_container_width=False
    ):
        update_prob()
        update_ans()
        st.switch_page("pages/wait_ai_grade.py")   # 跳转到你的目标页面

inject_pollers_for_active_jobs()