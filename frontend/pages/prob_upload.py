import streamlit as st
import requests
import os
from PIL import Image
import time
from utils import *
import hashlib
from datetime import datetime
import json

# --- 页面基础设置 ---
# 使用 "wide" 布局以获得更多空间，并设置页面标题和图标
st.set_page_config(
    page_title="上传作业 - 智能作业核查系统", 
    layout="wide",
    page_icon="📂"
)

KNOWLEDGE_BASE_DIR = "knowledge_bases"
KNOWLEDGE_BASE_CONFIG = "knowledge_base_config.json"

def save_knowledge_base_config():
    """保存知识库配置"""
    try:
        with open(KNOWLEDGE_BASE_CONFIG, 'w', encoding='utf-8') as f:
            json.dump(st.session_state.knowledge_bases, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        st.error(f"保存知识库配置失败: {e}")

def create_knowledge_base(name: str, description: str, category: str = "通用"):
    """创建新的知识库"""
    kb_id = f"kb_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(name.encode()).hexdigest()[:8]}"
    kb_path = os.path.join(KNOWLEDGE_BASE_DIR, kb_id)
    
    if not os.path.exists(kb_path):
        os.makedirs(kb_path)
    
    kb_info = {
        "id": kb_id,
        "name": name,
        "description": description,
        "category": category,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "file_count": 0,
        "total_size": 0,
        "files": {}
    }
    
    st.session_state.knowledge_bases[kb_id] = kb_info
    save_knowledge_base_config()
    return kb_id

def add_file_to_kb(kb_id: str, file_name: str, file_content: bytes, file_type: str = "unknown"):
    """向知识库添加文件"""
    if kb_id not in st.session_state.knowledge_bases:
        return False
    
    kb_path = os.path.join(KNOWLEDGE_BASE_DIR, kb_id)
    file_path = os.path.join(kb_path, file_name)
    
    try:
        # 保存文件
        with open(file_path, 'wb') as f:
            f.write(file_content)
        
        # 更新配置
        file_id = hashlib.md5((file_name + datetime.now().isoformat()).encode()).hexdigest()[:12]
        file_info = {
            "id": file_id,
            "name": file_name,
            "type": file_type,
            "size": len(file_content),
            "uploaded_at": datetime.now().isoformat(),
            "path": file_path
        }
        
        st.session_state.knowledge_bases[kb_id]["files"][file_id] = file_info
        st.session_state.knowledge_bases[kb_id]["file_count"] += 1
        st.session_state.knowledge_bases[kb_id]["total_size"] += len(file_content)
        st.session_state.knowledge_bases[kb_id]["updated_at"] = datetime.now().isoformat()
        
        save_knowledge_base_config()
        return True
    except Exception as e:
        st.error(f"添加文件失败: {e}")
        return False
    
initialize_session_state()

# 在每个页面的顶部调用这个函数
load_custom_css()

def render_header():
    """渲染页面头部"""
    col1, _, col2 = st.columns([8,50,8])
    col = st.columns(1)[0]

    with col1:
        st.page_link("main.py", label="返回首页", icon="🏠")
    
    with col2:
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


# --- 后端服务地址 ---
# BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000/hw_upload")

# --- 初始化会话状态 ---
# if 'prob_data' not in st.session_state:
#     st.session_state.prob_data = None
st.session_state.prob_data = None

# 如果数据已处理，直接跳转，避免重复上传
# if st.session_state.prob_data:
#     st.switch_page("pages/problems.py")

# --- 页面标题和简介 ---
# st.title("🚀 智能作业核查系统")
# st.markdown("高效、智能、全面——您的自动化教学助理。")
# st.markdown("---")


# --- 作业上传核心功能区 ---
st.markdown('<div class="card">', unsafe_allow_html=True)
st.header("📂 上传作业题目")
st.caption("请将本次作业的题目文件上传。")

uploaded_prob_file = st.file_uploader(
    "上传作业题目",
    type=['pdf', 'docx', 'txt', 'md'],
    help="提供标准作业题目，AI将自动识别题目类型。"
)
if uploaded_prob_file is not None:
    st.success(f"文件 '{uploaded_prob_file.name}' 已选择。")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("---")

# --- 高级选项配置区 (默认展开) ---
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.header("⚙️ 高级选项配置")

    # --- 新增：多模型协同批改设置 ---
    st.subheader("🤖 多模型协同批改")
    st.caption("引入多个专家模型联合批改，解决不同学科、交叉学科的复杂问题评估。")

    # 预设可选的AI模型列表
    available_models = ["Gemini", "ChatGPT", "DeepSeek", "ZhiPuAI (智谱清言)", "Claude"]
    
    selected_models = st.multiselect(
        "选择用于协同批改的AI模型 (可多选)",
        options=available_models,
        default=["Gemini", "ChatGPT"],  # 默认选中两个主流模型
        help="SmarTAI将为题目匹配最相关的专家模型，并根据各模型基于知识库给出的置信度，动态汇总其评分意见。"
    )

    # 初始化或更新模型权重
    if 'ai_weights' not in st.session_state:
        st.session_state.ai_weights = {}

    # 仅当用户选择了模型后，才显示权重设置
    if selected_models:
        st.markdown("##### 各模型权重配置")
        
        # 使用字典来存储权重，以便于后续处理
        current_weights = {}
        
        # 为了更好的布局，每行最多显示两个滑块
        cols = st.columns(2)
        col_idx = 0
        
        for model in selected_models:
            with cols[col_idx]:
                # 固定权重为50，不可滑动
                st.slider(
                    f"'{model}' 权重",
                    min_value=0,
                    max_value=100,
                    value=50,
                    key=f"weight_{model}",
                    disabled=True  # 禁用滑块
                )
                current_weights[model] = 50  # 固定设置为50
            # 切换到下一列
            col_idx = (col_idx + 1) % 2
        
        # 更新session_state中的权重记录
        st.session_state.ai_weights = current_weights
        
        st.info("提示：所有模型权重已固定为50，系统将根据各模型对题目的置信度自动调整最终评分。")
    else:
        st.warning("请至少选择一个AI模型以进行批改。")
    
    st.markdown("---")


    # --- 评分与批改设置 (原代码，可稍作标题调整以更好地区分) ---
    st.subheader("📝 评分基准设置")

    # 上传参考答案
    uploaded_answer_file = st.file_uploader(
        "上传参考答案 (可选)",
        help="提供标准答案文件，AI将以此为重要基准进行批改。",
        type=['pdf', 'docx', 'txt', 'md']
    )

    # 评分细则
    scoring_method = st.radio(
        "评分细则模式",
        ("预设严格度", "自定义评分细则"),
        horizontal=True,
        help="选择一个预设的评分标准，或提供详细的评分说明。"
    )

    if scoring_method == "预设严格度":
        scoring_strictness = st.select_slider(
            "选择评分严格度",
            options=["宽松", "适中", "严格"],
            value="适中"
        )
    else:
        st.info("您可选择在下方文本框中描述评分点，或直接上传包含评分细则的文件。")
        scoring_details_text = st.text_area(
            "请用自然语言描述您的评分要求",
            placeholder="例如：第一题占30分，其中步骤正确得10分，计算准确得10分，结果正确得10分..."
        )
        scoring_details_file = st.file_uploader(
            "或上传评分细则文件 (可选)",
            type=['pdf', 'docx', 'txt', 'md']
        )

    st.markdown("---")

    # --- 编程题专项设置 ---
    st.subheader("💻 编程题专项设置")
    uploaded_test_cases = st.file_uploader(
        "上传测试用例 (可选)",
        help="上传包含测试输入和预期输出的文件（如 .in, .out, .txt），用于代码题的自动评测。",
        accept_multiple_files=True
    )
    st.caption("ℹ️ 如果不上传，系统将尝试根据题目要求自动生成通用测试数据。")

    st.markdown("---")

    # --- 专业知识库配置 ---
    st.subheader("📚 配置专业知识库")
    st.caption("上传相关教材、讲义或参考资料，AI 将在分析和批改时参考这些内容，以提供更专业的反馈。")

    kb_choice = st.radio(
        "知识库选项",
        ("不使用知识库", "使用现有知识库", "新建知识库"),
        horizontal=True
    )

    if kb_choice == "使用现有知识库":
        # 从 session_state 动态获取知识库列表
        existing_kb_list = list(st.session_state.knowledge_bases.values())
        if not existing_kb_list:
            st.warning("暂无可用知识库，请先新建一个。")
        else:
            # 显示名称供选择，但背后使用ID
            kb_options = {kb["id"]: kb["name"] for kb in existing_kb_list}
            selected_kb_id = st.selectbox(
                "选择一个已有的知识库",
                options=list(kb_options.keys()),
                format_func=lambda kb_id: kb_options[kb_id]
            )
            if selected_kb_id:
                st.success(f"已选择知识库: **{kb_options[selected_kb_id]}**")

    elif kb_choice == "新建知识库":
        st.markdown("##### 1. 选择分类")
        categories = ["通用", "计算机科学", "数学", "物理", "化学", "生物", "其他"]
        # 使用 session_state 来保存用户的选择，以便在页面刷新后保留
        if 'category_selection' not in st.session_state:
            st.session_state.category_selection = "通用"
        
        st.selectbox(
            "分类", 
            categories, 
            key="category_selection" # 绑定到 session_state
        )

        with st.form("new_kb_form", clear_on_submit=True):
            st.markdown("#### 2. 填写详细信息")
            new_kb_name = st.text_input("新知识库名称*", placeholder="例如：高等数学-第五章-知识点")
            new_kb_desc = st.text_area("知识库描述 (可选)", placeholder="简要描述知识库包含的内容、课程、章节等。")
            
            # 使用 selectbox 提供更好的分类选择
            # categories = ["通用", "计算机科学", "数学", "物理", "化学", "生物", "其他"]
            # category_selection = st.selectbox("分类", categories)
            if st.session_state.category_selection == "其他":
                new_kb_category = st.text_input("自定义分类", placeholder="输入自定义分类...")
            else:
                new_kb_category = st.session_state.category_selection

            knowledge_files = st.file_uploader(
                "上传知识库文件 (可多选)",
                accept_multiple_files=True,
                type=['pdf', 'docx', 'txt', 'md']
            )
            
            submitted = st.form_submit_button("✅ 确认创建知识库", type="primary", use_container_width=True)

            if submitted:
                if not new_kb_name:
                    st.error("知识库名称不能为空。")
                elif not knowledge_files:
                    st.error("请至少上传一个知识库文件。")
                else:
                    final_category = new_kb_category or "通用"
                    with st.spinner(f"正在创建知识库 '{new_kb_name}'..."):
                        # 调用您已有的函数来创建知识库
                        kb_id = create_knowledge_base(new_kb_name, new_kb_desc, final_category)
                        
                        # 如果有上传文件，则添加到知识库中
                        if knowledge_files:
                            success_count = 0
                            for uploaded_file in knowledge_files:
                                file_content = uploaded_file.read()
                                file_type = uploaded_file.type or "unknown"
                                if add_file_to_kb(kb_id, uploaded_file.name, file_content, file_type):
                                    success_count += 1
                            st.success(f"✅ 知识库 '{new_kb_name}' 创建成功，并已上传 {success_count} 个文件！")
                        else:
                            st.success(f"知识库 '{new_kb_name}' 创建成功！")

                    st.caption("提示：创建的知识库将保存至您的账户，方便未来重复使用。")

st.markdown('</div>', unsafe_allow_html=True)


# --- 确认与提交区 ---
st.markdown("---")
st.header("✅ 确认并开始识别题目")
st.info("请检查以上信息。点击下方按钮后，系统将开始处理您的文件。")

# 当用户上传了作业文件后，才激活确认按钮
if uploaded_prob_file is not None:
    if st.button("确认信息，开始智能识别题目", type="primary", use_container_width=True):
        with st.spinner("正在上传并请求AI分析，请耐心等待几分钟..."):
            # 准备要发送的文件
            files_to_send = {
                "file": (uploaded_prob_file.name, uploaded_prob_file.getvalue(), uploaded_prob_file.type)
            }
            # (这里可以添加逻辑来处理其他上传的文件，例如答案、测试用例等)
            st.session_state.task_name=uploaded_prob_file.name
            try:
                # TODO: 实际使用时，你需要根据后端API来组织和发送所有数据
                response = requests.post(f"{st.session_state.backend}/prob_preview", files=files_to_send, timeout=600)
                response.raise_for_status()
                
                problems = response.json()                            
                # st.session_state.prob_data = {q['q_id']: q for q in problems.get('problems', [])}   #以q_id为key索引
                st.session_state.prob_data = problems
                           
                st.success("✅ 文件上传成功，后端开始处理！即将跳转至结果预览页面...")
                time.sleep(1) # 短暂显示成功信息
                st.switch_page("pages/problems.py")

            except requests.exceptions.RequestException as e:
                st.error(f"网络或服务器错误: {e}")
            except Exception as e:
                st.error(f"发生未知错误: {e}")
else:
    # 如果用户还未上传文件，则按钮禁用
    st.button("确认信息，开始智能核查", type="primary", use_container_width=True, disabled=True)
    st.warning("请先在上方上传本次作业题目。")

inject_pollers_for_active_jobs()