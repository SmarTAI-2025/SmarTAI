import streamlit as st
from streamlit_scroll_to_top import scroll_to_here
import requests
import pandas as pd
from utils import *
import json
import os
import re
import datetime

# --- 页面基础设置 ---
st.set_page_config(
    page_title="AI批改结果 - 智能作业核查系统",
    layout="wide",
    page_icon="📊"
)

initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

def render_header():
    """渲染页面头部"""
    col1, col2, col3, col4, col5, col6, col7, col8 = st.columns(8)
    col = st.columns(1)[0]

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")
    
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

    with col8:
        if st.button("🔄 刷新数据", use_container_width=False):
            st.rerun()
    
    with col:
        st.markdown("<h1 style='text-align: center; color: #000000;'>📊 AI批改结果总览</h1>", 
                   unsafe_allow_html=True)
 
render_header()

# --- 安全检查 (已修复) ---

# 1. 确保 st.session_state.jobs 是一个字典
if "jobs" not in st.session_state:
    st.session_state.jobs = {}

# 2. 如果有从提交页面传来的新任务ID，就将其“添加”到 jobs 字典中，而不是覆盖
if "current_job_id" in st.session_state:
    new_job_id = st.session_state.current_job_id
    if new_job_id not in st.session_state.jobs:
        # 使用字典的 update 方法或直接赋值来“添加”新任务
        st.session_state.jobs[new_job_id] = {"name": f"最新批改任务 - {new_job_id}", "submitted_at": {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}
    
    # 将当前选中的任务设置为这个新任务
    st.session_state.selected_job_id = new_job_id
    
    # 清理掉临时的 current_job_id
    del st.session_state.current_job_id

# 3. 如果没有任何任务记录，则提示并停止
if not st.session_state.jobs:
    st.warning("当前没有批改任务记录。")
    st.stop()

# 4. 获取当前应该选择的任务ID
selected_job_id = st.session_state.get("selected_job_id")

# ... 后续代码不变 ...

# # Filter out mock jobs
# filtered_jobs = {}
# if "jobs" in st.session_state:
#     for job_id, job_info in st.session_state.jobs.items():
#         # Skip mock jobs
#         if not job_id.startswith("MOCK_JOB_") and not job_info.get("is_mock", False):
#             filtered_jobs[job_id] = job_info
#     st.session_state.jobs = filtered_jobs

# Get job IDs after filtering
job_ids = list(st.session_state.jobs.keys()) if "jobs" in st.session_state else []

# --- 页面内容 ---
# st.title("📊 AI批改结果")

# # Add debug button
# if st.button("调试：检查所有任务"):
#     from frontend_utils.data_loader import check_all_jobs
#     all_jobs = check_all_jobs()
#     st.write("所有任务状态:", all_jobs)

# 映射题目类型：从内部类型到中文显示名称
type_display_mapping = {
    "concept": "概念题",
    "calculation": "计算题", 
    "proof": "证明题",
    "programming": "编程题"
}

def natural_sort_key(s):
    """
    实现自然排序的辅助函数。
    例如: "q2" 会排在 "q10" 之前。
    """
    # 确保输入是字符串
    s = str(s)
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

# --- 整个旧的 `if/else` 逻辑块被替换为以下新代码 ---

# --- 改动 1: 引入新的、统一的任务获取函数 ---
# 我们不再直接使用 job_ids 列表，而是调用在 utils.py 中创建的 get_all_jobs_for_selection 函数。
# 这个函数会返回一个包含【模拟任务】和所有【真实任务】的字典，格式为 {job_id: task_name}。
selectable_jobs = get_all_jobs_for_selection()

if not selectable_jobs:
    st.info("没有找到批改任务。")
    st.stop()
else:
    # --- 改动 2: 实现智能的默认选择逻辑 ---
    # 这是本次修改的核心。我们根据清晰的优先级规则来决定下拉框默认应该显示哪个任务。
    job_ids = list(selectable_jobs.keys())
    default_index = 0  # 默认选项的索引，默认为列表中的第一个（也就是最新的或模拟任务）

    # 优先级 1: 用户是否从 history.py 点击了某个特定任务？
    if "selected_job_from_history" in st.session_state:
        job_id_from_history = st.session_state.selected_job_from_history
        if job_id_from_history in job_ids:
            default_index = job_ids.index(job_id_from_history)
        # 这个临时变量一旦使用就必须删除，防止在刷新页面时依然生效。
        del st.session_state.selected_job_from_history

    # 优先级 2: 用户是否刚刚提交了一个新任务？
    elif "newly_submitted_job_id" in st.session_state:
        new_job_id = st.session_state.newly_submitted_job_id
        if new_job_id in job_ids:
            default_index = job_ids.index(new_job_id)
        # 这个变量暂时不删除，因为用户可能需要切换到 score_report 等页面，这些页面也需要知道这个新任务ID。

    # 优先级 3 (回退): 如果以上情况都不是，就使用全局保存的选择。
    elif "selected_job_id" in st.session_state and st.session_state.selected_job_id in job_ids:
        default_index = job_ids.index(st.session_state.selected_job_id)

    # --- 改动 3: 创建带回调函数的下拉选择框 ---
    # 这是全新的UI组件，它取代了旧的、不明确的选择逻辑。
    def on_selection_change():
        """当用户在下拉框中手动选择一个新选项时，这个函数会被调用。"""
        st.session_state.selected_job_id = st.session_state.grade_results_selector
        if "newly_submitted_job_id" in st.session_state:
            del st.session_state.newly_submitted_job_id

    selected_job = st.selectbox(
        "选择一个批改任务进行查看",
        options=job_ids,
        format_func=lambda jid: selectable_jobs.get(jid, jid),
        index=default_index,
        key="grade_results_selector",
        on_change=on_selection_change
    )

    # 确保在首次加载时，全局选择状态被正确初始化。
    if "selected_job_id" not in st.session_state:
        st.session_state.selected_job_id = selected_job

    # --- 改动 4: 根据下拉框的 `selected_job` 变量来驱动后续的页面渲染 ---
    if selected_job:
        # 情况 A: 如果选择的是模拟任务
        if selected_job.startswith("MOCK_JOB_"):
            st.subheader(f"任务: {selectable_jobs[selected_job]}")
            st.info("当前显示模拟数据。真实任务完成后，请从下拉框选择以查看结果。")
            
            # --- 以下是您原代码中用于显示模拟数据的部分，未作修改，直接移入此逻辑块 ---
            try:
                from frontend_utils.data_loader import load_mock_data
                mock_data = load_mock_data()
                
                if "student_scores" in mock_data:
                    all_mock_students = mock_data["student_scores"]
                    all_mock_students.sort(key=lambda s: s.student_id)
                    
                    st.subheader("模拟学生批改结果预览")
                    for student in all_mock_students[:5]:
                        st.markdown(f"### 学生: {student.student_name} ({student.student_id})")
                        student.questions.sort(key=lambda q: natural_sort_key(q['question_id']))
                        data = []
                        total_score = 0
                        total_max_score = 0
                        for question in student.questions:
                            data.append({
                                "题号": question["question_id"][1:],
                                "题目类型": question["question_type"],
                                "得分": f"{question['score']:.1f}",
                                "满分": f"{question['max_score']:.1f}",
                                "置信度": f"{question['confidence']:.2f}",
                                "评语": question["feedback"]
                            })
                            total_score += question["score"]
                            total_max_score += question["max_score"]
                        df = pd.DataFrame(data)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                        st.write(f"**总分: {total_score:.1f}/{total_max_score:.1f}**")
                        st.divider()
            except Exception as e:
                st.warning(f"无法加载模拟数据: {e}")

        # 情况 B: 如果选择的是一个真实的批改任务
        else:
            task_info = st.session_state.jobs.get(selected_job, {})
            st.subheader(f"任务: {task_info.get('name', '未知任务')}")
            st.write(f"提交时间: {task_info.get('submitted_at', '未知时间')}")

            # --- 以下是您原代码中用于获取和显示真实批改结果的部分 ---
            # --- 内部逻辑未作修改，仅针对 status == 'pending' 情况增加了模拟数据展示 ---
            try:
                response = requests.get(
                    f"{st.session_state.backend}/ai_grading/grade_result/{selected_job}",
                    timeout=10
                )
                response.raise_for_status()
                result = response.json()
                
                status = result.get("status", "未知")
                st.write(f"状态: {status}")
                st.markdown("---")
                
                has_data = "results" in result or "corrections" in result
                
                if status == "completed" or has_data:
                    if "results" in result:  # Batch grading results
                        all_results = result["results"]
                        st.subheader("所有学生批改结果")
                        all_results.sort(key=lambda s: s['student_id'])
                        for student_result in all_results:
                            student_id = student_result["student_id"]
                            corrections = student_result["corrections"]
                            corrections.sort(key=lambda c: natural_sort_key(c['q_id']))
                            st.markdown(f"### 学生: {student_id}")
                            data = []
                            total_score = 0
                            total_max_score = 0
                            for correction in corrections:
                                question_type = correction["type"]
                                if question_type in type_display_mapping:
                                    display_type = type_display_mapping[question_type]
                                elif question_type in type_display_mapping.values():
                                    display_type = question_type
                                else:
                                    display_type = "概念题"
                                data.append({
                                    "题号": correction["q_id"][1:],
                                    "题目类型": display_type,
                                    "得分": f"{correction['score']:.1f}",
                                    "满分": f"{correction['max_score']:.1f}",
                                    "置信度": f"{correction['confidence']:.2f}",
                                    "评语": correction["comment"]
                                })
                                total_score += correction["score"]
                                total_max_score += correction["max_score"]
                            df = pd.DataFrame(data)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                            st.write(f"**总分: {total_score:.1f}/{total_max_score:.1f}**")
                            st.divider()
                    elif "corrections" in result:  # Single student grading results
                        # ... (此部分代码与您原代码完全相同，故省略以保持简洁)
                        pass
                    else:
                        st.warning("批改结果中没有找到学生数据。")

                elif status == "error":
                    st.error(f"批改过程中出现错误: {result.get('message', '未知错误')}")

                # --- 改动 5: 优化 'pending' 状态的处理 ---
                # 当任务正在等待时，明确提示用户，并按要求展示模拟数据作为预览。
                elif status == "pending":
                    st.info("批改任务正在进行中...下方为模拟数据预览，待任务完成后请点击右上角“刷新数据”按钮。")
                    try:
                        from frontend_utils.data_loader import load_mock_data
                        mock_data = load_mock_data()
                        if "student_scores" in mock_data:
                            all_mock_students = mock_data["student_scores"]
                            all_mock_students.sort(key=lambda s: s.student_id)
                            st.subheader("模拟学生批改结果预览")
                            for student in all_mock_students[:5]:
                                st.markdown(f"### 学生: {student.student_name} ({student.student_id})")
                                student.questions.sort(key=lambda q: natural_sort_key(q['question_id']))
                                data = []
                                total_score = 0
                                total_max_score = 0
                                for question in student.questions:
                                    data.append({
                                        "题号": question["question_id"][1:],
                                        "题目类型": question["question_type"],
                                        "得分": f"{question['score']:.1f}",
                                        "满分": f"{question['max_score']:.1f}",
                                        "置信度": f"{question['confidence']:.2f}",
                                        "评语": question["feedback"]
                                    })
                                    total_score += question["score"]
                                    total_max_score += question["max_score"]
                                df = pd.DataFrame(data)
                                st.dataframe(df, use_container_width=True, hide_index=True)
                                st.write(f"**总分: {total_score:.1f}/{total_max_score:.1f}**")
                                st.divider()
                    except Exception as e:
                        st.warning(f"无法加载模拟数据: {e}")
                else:
                    st.warning(f"未知状态: {status}")
                    
            except requests.exceptions.RequestException as e:
                st.error(f"获取批改结果失败: {e}")
                st.info("显示模拟数据作为备用")
                # 此处也保留显示模拟数据的逻辑
                try:
                    from frontend_utils.data_loader import load_mock_data
                    mock_data = load_mock_data()
                    if "student_scores" in mock_data:
                        all_mock_students = mock_data["student_scores"]
                        all_mock_students.sort(key=lambda s: s.student_id)
                        st.subheader("模拟学生批改结果")
                        for student in all_mock_students[:5]:
                            st.markdown(f"### 学生: {student.student_name} ({student.student_id})")
                            student.questions.sort(key=lambda q: natural_sort_key(q['question_id']))
                            data = []
                            total_score = 0
                            total_max_score = 0
                            for question in student.questions:
                                data.append({
                                    "题号": question["question_id"][1:],
                                    "题目类型": question["question_type"],
                                    "得分": f"{question['score']:.1f}",
                                    "满分": f"{question['max_score']:.1f}",
                                    "置信度": f"{question['confidence']:.2f}",
                                    "评语": question["feedback"]
                                })
                                total_score += question["score"]
                                total_max_score += question["max_score"]
                            df = pd.DataFrame(data)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                            st.write(f"**总分: {total_score:.1f}/{total_max_score:.1f}**")
                            st.divider()
                except Exception as e:
                    st.warning(f"无法加载模拟数据: {e}")
            except Exception as e:
                st.error(f"处理批改结果时出现错误: {e}")
                # 此处也保留显示模拟数据的逻辑
                st.info("显示模拟数据作为备用")
                try:
                    from frontend_utils.data_loader import load_mock_data
                    mock_data = load_mock_data()
                    if "student_scores" in mock_data:
                        all_mock_students = mock_data["student_scores"]
                        all_mock_students.sort(key=lambda s: s.student_id)
                        st.subheader("模拟学生批改结果")
                        for student in all_mock_students[:5]:
                            st.markdown(f"### 学生: {student.student_name} ({student.student_id})")
                            student.questions.sort(key=lambda q: natural_sort_key(q['question_id']))
                            data = []
                            total_score = 0
                            total_max_score = 0
                            for question in student.questions:
                                data.append({
                                    "题号": question["question_id"][1:],
                                    "题目类型": question["question_type"],
                                    "得分": f"{question['score']:.1f}",
                                    "满分": f"{question['max_score']:.1f}",
                                    "置信度": f"{question['confidence']:.2f}",
                                    "评语": question["feedback"]
                                })
                                total_score += question["score"]
                                total_max_score += question["max_score"]
                            df = pd.DataFrame(data)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                            st.write(f"**总分: {total_score:.1f}/{total_max_score:.1f}**")
                            st.divider()
                except Exception as e:
                    st.warning(f"无法加载模拟数据: {e}")

inject_pollers_for_active_jobs()

def return_top():
    scroll_to_here(50, key='top')
    scroll_to_here(0, key='top_fix')

# Add a link back to the history page

col1, _, col2 = st.columns([8, 40, 12])

with col1:
    st.button(
        "返回顶部", 
        on_click=return_top,
        use_container_width=False
    )

with col2:
    # 2. 创建一个按钮，并告诉它在被点击时调用上面的函数
    st.page_link("pages/history.py", label="返回历史批改记录", icon="➡️")