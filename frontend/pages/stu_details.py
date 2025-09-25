# pages/stu_details.py

import streamlit as st
from streamlit_scroll_to_top import scroll_to_here
from utils import *

# --- 页面基础设置 (建议添加) ---
st.set_page_config(
    page_title="学生作业详情 - 智能作业核查系统",
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
        st.markdown("<h1 style='text-align: center; color: #000000;'>📝 学生作业作答详情</h1>", 
                   unsafe_allow_html=True)
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

# 检查是否有学生被选中，防止用户直接访问此页面
if 'selected_student_id' not in st.session_state or not st.session_state.get('selected_student_id'):
    st.warning("请先从“学生作业总览”页面选择一个学生。")
    # st.page_link("pages/stu_preview.py", label="返回总览页面", icon="📖")
    st.stop()


# # --- 滚动逻辑 ---
# # 每次进入详情页时，自动滚动到顶部
# scroll_to_here(50, key='top')
# scroll_to_here(0, key='top_fix')


# --- 侧边栏导航 (与总览页保持一致) ---
with st.sidebar:
    st.header("导航")
    
    # st.page_link("pages/problems.py", label="题目识别概览", icon="📝")
    st.page_link("pages/stu_preview.py", label="学生作答总览", icon="📝")

    with st.expander("按学生查看", expanded=True):
        student_list = sorted(list(st.session_state.processed_data.keys()))
        
        # 获取当前正在查看的学生ID
        current_sid = st.session_state.get('selected_student_id')

        if not student_list:
            st.caption("暂无学生数据")
        else:
            # 定义回调函数，用于切换查看不同的学生
            def select_student(sid):
                st.session_state['selected_student_id'] = sid
                # 由于已经在详情页，切换学生只需 rerun 即可，无需切换页面
                # st.rerun()
                scroll_to_here(50, key='top')
                scroll_to_here(0, key='top_fix')

            for sid in student_list:
                # 判断当前按钮是否为正在查看的学生
                is_selected = (sid == current_sid)
                st.button(
                    sid, 
                    key=f"btn_student_{sid}", 
                    on_click=select_student,
                    args=(sid,),
                    disabled=is_selected, # 禁用当前已选中的学生按钮
                    use_container_width=True,
                    # type='primary'
                )


# --- 主页面内容：学生详情视图 ---

def render_student_view(student_id):
    """
    渲染单个学生作业详情的视图，并提供对每个答案的编辑功能。
    """
    # 从 session_state 中获取题目数据和指定学生的数据
    problems_data = st.session_state.prob_data
    stu_data = st.session_state.processed_data.get(student_id, {})

    stu_name = stu_data.get("stu_name", "未知姓名")
    st.header(f"🎓 学生: {student_id} - {stu_name}")

    answers = stu_data.get('stu_ans', [])

    if not answers:
        st.warning("未找到该学生的任何答案提交记录。")
        return
        
    # 遍历该学生的所有答案
    for ans in answers:
        q_id = ans.get('q_id')
        question_info = problems_data.get(q_id)
        
        # 如果找不到对应的题目信息，则跳过此答案
        if not question_info: 
            continue
        
        with st.container(border=True):
            # --- 初始化独立的 Session State ---
            # 为每个答案创建一个唯一的编辑状态key
            edit_answer_key = f"edit_answer_{student_id}_{q_id}"
            if edit_answer_key not in st.session_state:
                st.session_state[edit_answer_key] = False

            # --- 模式1: 编辑学生答案 ---
            if st.session_state[edit_answer_key]:
                st.markdown(f"**正在编辑题目 {question_info.get('number', '')} 的解答:**")
                
                # 注意：编程题的 content 是 dict，其他是 str，需要分别处理
                current_content = ans.get('content', '')
                if isinstance(current_content, dict):
                    # 简化处理：对于编程题，我们只编辑第一个文件的代码
                    # 您也可以根据需要设计更复杂的编辑逻辑，比如用 st.tabs 显示多个文件
                    first_file = next(iter(current_content.keys()), None)
                    if first_file:
                        new_answer_content = st.text_area(
                            f"编辑代码文件: {first_file}", 
                            value=current_content[first_file], 
                            key=f"ans_content_{student_id}_{q_id}", 
                            height=250
                        )
                    else:
                        st.info("该编程题无文件内容可编辑。")
                        new_answer_content = "" # 避免下面保存时出错
                else:
                    new_answer_content = st.text_area(
                        "编辑学生答案 (支持 LaTeX)", 
                        value=str(current_content), 
                        key=f"ans_content_{student_id}_{q_id}", 
                        height=150
                    )
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ 保存答案", key=f"save_ans_btn_{student_id}_{q_id}", type="primary", use_container_width=True):
                        # 更新数据到 session_state
                        # 同样需要区分编程题和普通题型
                        if isinstance(current_content, dict) and first_file:
                             st.session_state.processed_data[student_id]['stu_ans'][answers.index(ans)]['content'][first_file] = new_answer_content
                        else:
                            st.session_state.processed_data[student_id]['stu_ans'][answers.index(ans)]['content'] = new_answer_content
                        
                        st.session_state.ans_changed = True
                        st.session_state[edit_answer_key] = False
                        st.rerun()
                with col2:
                    if st.button("❌ 取消", key=f"cancel_ans_btn_{student_id}_{q_id}", use_container_width=True):
                        st.session_state[edit_answer_key] = False
                        st.rerun()

            # --- 模式2: 正常显示 ---
            else:
                col1, col2 = st.columns([0.85, 0.15])
                with col1:
                    # 显示题干
                    st.markdown(f"**题目 {question_info.get('number', '')}:**")
                    stem_text = question_info.get('stem', '题干内容为空').strip()
                    if stem_text.startswith('$') and stem_text.endswith('$'):
                        st.latex(stem_text.strip('$'))
                    else:
                        st.markdown(stem_text)
                    
                    # 显示需要人工处理的标记
                    if ans.get('flag'):
                        for flag in ans['flag']:
                            st.error(f"🚩 **需人工处理**: {flag}")
                    
                    # 显示学生答案
                    st.markdown("**学生答案:**")
                    q_type = question_info.get('type')
                    content = ans.get('content')
                    
                    if q_type == "编程题" and isinstance(content, dict):
                        if content.keys():
                            file_to_show = st.selectbox("选择代码文件", options=list(content.keys()), key=f"file_{student_id}_{q_id}", label_visibility="collapsed")
                            if file_to_show:
                                st.code(content[file_to_show], language="python")
                        else:
                            st.info("该学生未提交此编程题的文件。")
                    else:
                        try:
                            content_str = str(content).strip()
                            if content_str.startswith('$') and content_str.endswith('$'):
                                st.latex(content_str.strip('$'))
                            else:
                                st.markdown(content_str, unsafe_allow_html=True)
                        except Exception:
                            st.text(str(content))

                with col2:
                    if st.button("✏️ 编辑答案", key=f"edit_ans_btn_{student_id}_{q_id}"):
                        st.session_state[edit_answer_key] = True
                        st.rerun()





# 获取当前选定的学生ID并渲染其视图
selected_student_id = st.session_state.get('selected_student_id')
render_student_view(selected_student_id)

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

def return_top():
    scroll_to_here(50, key='top')
    scroll_to_here(0, key='top_fix')
# 使用列布局将按钮推到右侧 (这部分和你的代码一样)
col1, _, col2 = st.columns([8, 40, 8])

with col1:
    st.button(
        "返回顶部", 
        on_click=return_top,
        use_container_width=False
    )

with col2:
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