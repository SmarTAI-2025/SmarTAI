import streamlit as st
# 假设 utils.py 和你的主 app 在同一级目录
from utils import * 

# --- 页面基础设置 (建议添加) ---
st.set_page_config(
    page_title="题目识别概览 - 智能作业核查系统",
    layout="wide",
    page_icon="📝"
)

initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

def render_header():
    """渲染页面头部"""
    col1, col2, _, col3 = st.columns([8,12,30,8])
    col = st.columns(1)[0]

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")

    with col2:
        st.page_link("pages/prob_upload.py", label="重新上传作业题目", icon="📤")
    
    with col3:
        st.page_link("pages/history.py", label="历史记录", icon="🕒")
    
    with col:
        st.markdown("<h1 style='text-align: center; color: #000000;'>📖 题目识别概览</h1>", 
                   unsafe_allow_html=True)
        st.markdown("---")
        
render_header()
# --- 安全检查 ---
# 检查必要的数据是否已加载st.session_state.prob_data
if 'prob_data' not in st.session_state or not st.session_state.get('prob_data'):
    st.warning("请先在“作业题目上传”页面上传并作业题目文件。")
    # st.page_link("pages/prob_upload.py", label="返回上传页面", icon="📤")
    st.stop()


# --- 渲染函数 (从原代码复制过来，无需修改) ---
def render_question_overview():
    # st.header("📝 题目识别概览")
    st.caption("您可以直接在左侧下拉框中修改题目类型，或点击编辑按钮修改题干与评分标准。")
    # problems = st.session_state.prob_data.get('problems', [])
    problems = st.session_state.prob_data

    if not problems:
        st.info("数据中没有识别到题目信息。")
        return

    # for i, q in enumerate(problems):
    #     # 使用唯一且稳定的ID作为key的基础
    #     q_id = q.get('q_id', f"question_{i}")
    
    for q_id, q in problems.items():
        with st.container(border=True):
            # 为题干编辑和评分标准编辑分别创建独立的session state
            edit_stem_key = f"edit_stem_{q_id}"
            edit_criterion_key = f"edit_criterion_{q_id}"
            if edit_stem_key not in st.session_state:
                st.session_state[edit_stem_key] = False
            if edit_criterion_key not in st.session_state:
                st.session_state[edit_criterion_key] = False

            # --- 模式1: 编辑题干 ---
            if st.session_state[edit_stem_key]:
                st.markdown(f"**正在编辑题目: {q.get('number', '')}**")
                new_stem = st.text_area("编辑题干 (支持 LaTeX)", value=q.get('stem', ''), key=f"q_stem_{q_id}", height=150)
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ 保存题干", key=f"save_stem_btn_{q_id}", type="primary", use_container_width=True):
                        st.session_state.prob_data[q_id]['stem'] = new_stem
                        st.session_state.prob_changed = True
                        st.session_state[edit_stem_key] = False
                        st.rerun()
                with col2:
                    if st.button("❌ 取消", key=f"cancel_stem_btn_{q_id}", use_container_width=True):
                        st.session_state[edit_stem_key] = False
                        st.rerun()

            # --- 模式2: 编辑评分标准 ---
            elif st.session_state[edit_criterion_key]:
                st.markdown(f"**正在编辑评分标准: {q.get('number', '')}**")
                new_criterion = st.text_area("编辑评分标准", value=q.get('criterion', ''), key=f"q_criterion_{q_id}", height=100)
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ 保存标准", key=f"save_criterion_btn_{q_id}", type="primary", use_container_width=True):
                        st.session_state.prob_data[q_id]['criterion'] = new_criterion
                        st.session_state.prob_changed = True
                        st.session_state[edit_criterion_key] = False
                        st.rerun()
                with col2:
                    if st.button("❌ 取消", key=f"cancel_criterion_btn_{q_id}", use_container_width=True):
                        st.session_state[edit_criterion_key] = False
                        st.rerun()
            
            # --- 模式3: 正常显示 ---
            else:
                col1, col2, col3 = st.columns([0.2, 0.65, 0.15])
                with col1:
                    q_types = ["概念题", "计算题", "证明题", "推理题", "编程题", "其他"]
                    current_type = q.get('type')
                    try:
                        current_type_index = q_types.index(current_type) if current_type in q_types else 0
                    except ValueError:
                        current_type_index = 0  # 安全保护

                    new_type = st.selectbox("题目类型", options=q_types, index=current_type_index, key=f"q_type_{q_id}", label_visibility="collapsed")
                    # 如果类型发生变化，直接更新
                    if new_type != st.session_state.prob_data[q_id]['type']:
                        st.session_state.prob_data[q_id]['type'] = new_type
                        st.session_state.prob_changed = True
                        st.rerun()

                with col2:
                    st.markdown(f"**{q.get('number', 'N/A')}:** {q.get('stem', '题干内容为空')}")
                    # 新增：显示评分标准
                    st.markdown(f"**评分标准:** *{q.get('criterion', '评分标准为空')}*")

                with col3:
                    if st.button("✏️ 编辑题干", key=f"edit_stem_btn_{q_id}"):
                        st.session_state[edit_stem_key] = True
                        st.rerun()
                    # 新增：编辑评分标准的按钮
                    if st.button("✏️ 编辑标准", key=f"edit_criterion_btn_{q_id}"):
                        st.session_state[edit_criterion_key] = True
                        st.rerun()



# --- 页面主逻辑 ---
render_question_overview()

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
col_spacer, col_button = st.columns([60, 8])

with col_button:
    # 2. 创建一个按钮，并告诉它在被点击时调用上面的函数
    if st.button(
        "✅ 确认题目", 
        on_click=start_ai_grading_and_navigate, 
        use_container_width=False # 让按钮填满列宽，视觉效果更好
    ):
        update_prob()
        update_ans()
        st.switch_page("pages/hw_upload.py")   # 跳转到你的目标页面

inject_pollers_for_active_jobs()