# utils.py
import streamlit as st
import streamlit.components.v1 as components
import json # 引入 json 库用于将 Python 列表转换为 JS 数组
import requests
import os
KNOWLEDGE_BASE_DIR = "knowledge_bases"
KNOWLEDGE_BASE_CONFIG = "knowledge_base_config.json"

def load_knowledge_base_config():
    """从 JSON 文件加载知识库配置到 session_state"""
    if os.path.exists(KNOWLEDGE_BASE_CONFIG):
        with open(KNOWLEDGE_BASE_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def load_custom_css(file_path=None):
    """
    从指定路径加载CSS文件并应用到Streamlit应用中。
    自动处理相对路径问题。
    """
    import os
    
    if file_path is None:
        # 获取当前文件的目录
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # 构建CSS文件的绝对路径
        file_path = os.path.join(current_dir, "static", "main.css")
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        st.error(f"CSS文件未找到: {file_path}")
    except Exception as e:
        st.error(f"加载CSS文件时出错: {str(e)}")

def initialize_session_state():
    """
    在每个页面顶部运行的辅助函数，用于初始化 session_state。
    如果某个键不存在，就为其设置一个初始值。
    """
    if "jobs" not in st.session_state:
        st.session_state.jobs = {}
    
    # --- 关键改动在这里 ---
    # 如果 'backend' 这个键不存在于 session_state 中，就设置它的初始/固定值
    if "backend" not in st.session_state:
        # 在这里硬编码你的后端地址
        st.session_state.backend = "http://localhost:8000" 
        
    if 'prob_changed' not in st.session_state:
        st.session_state.prob_changed = False

    if 'ans_changed' not in st.session_state:
        st.session_state.ans_changed = False

    if 'knowledge_bases' not in st.session_state:
        st.session_state.knowledge_bases = load_knowledge_base_config()
        
def update_prob():
    if st.session_state.get('prob_changed', False):
        st.info("检测到题目数据已修改，正在更新存储到后端...") # 友好提示
        try:
            requests.post(
                f"{st.session_state.backend}/human_edit/problems",
                json=st.session_state.prob_data
            )
            
            print("数据已成功保存到后端！") # 在终端打印日志
            st.toast("更改已成功保存！", icon="✅")

            # 保存成功后，重置标志位
            st.session_state.prob_changed = False
        except Exception as e:
            st.error(f"保存失败，错误信息: {e}")
            print(f"Error saving to DB: {e}") # 在终端打印错误

def update_ans():
    if st.session_state.get('ans_changed', False):
        st.info("检测到学生作答数据已修改，正在更新存储到后端...") # 友好提示
        try:
            requests.post(
                f"{st.session_state.backend}/human_edit/stu_ans",
                json=st.session_state.processed_data
            )
            
            print("数据已成功保存到后端！") # 在终端打印日志
            st.toast("更改已成功保存！", icon="✅")

            # 保存成功后，重置标志位
            st.session_state.prob_changed = False
        except Exception as e:
            st.error(f"保存失败，错误信息: {e}")
            print(f"Error saving to DB: {e}") # 在终端打印错误

def get_master_poller_html(jobs_json: str, backend_url: str) -> str:
    """
    生成一个"主"轮询脚本。
    这个脚本接收一个包含所有任务详细信息的 JSON 对象，
    并在内部为每个 job_id 启动轮询。
    """
    be = backend_url.rstrip("/")
    # jobs_json 现在是一个字典的JSON字符串，例如：
    # '{"job1":{"name":"file1.pdf", "submitted_at":"..."}, "job2":{...}}'
    return f"""
    <script>
    (function() {{
        const backend = '{be}';
        let jobsData; // <-- 变量名修改，以反映其为数据对象

        try {{
            jobsData = JSON.parse('{jobs_json}');
        }} catch (e) {{
            console.error("无法解析任务数据对象:", e);
            jobsData = {{}};
        }}

        // 获取所有待轮询的任务ID (即对象的键)
        const jobIds = Object.keys(jobsData);

        if (jobIds.length === 0) {{
            return;
        }}

        // 定义一个为单个任务启动轮询的函数
        // <-- 接收 job_id 和对应的任务详情对象
        const startPollingForJob = (jobId, taskDetails) => {{
            const completedKey = `job-completed-${{jobId}}`;

            if (sessionStorage.getItem(completedKey)) {{
                return;
            }}

            const intervalId = setInterval(async () => {{
                try {{
                    // 轮询的URL依然只使用 job_id
                    const resp = await fetch(backend + '/ai_grading/grade_result/' + jobId);
                    if (!resp.ok) return;

                    const data = await resp.json();
                    if (data && data.status === 'completed') {{
                        clearInterval(intervalId);
                        if (!sessionStorage.getItem(completedKey)) {{
                            // --- 核心修改：生成用户友好的弹窗消息 ---
                            const taskName = taskDetails.name || "未命名任务";
                            const submittedAt = taskDetails.submitted_at || "未知时间";
                            alert(`您于 [${{submittedAt}}] 提交的任务："${{taskName}}"已成功完成！\\n请前往“历史批改记录”-“批改结果”查看，或直接查看[报告]和[分析]。\\n如果您当前正在AI批改结果总览窗口，请手动点击右上角“刷新数据”按钮以查看最新批改数据！`);
                            // 标记为完成，防止重复弹窗
                            sessionStorage.setItem(completedKey, 'true');
                            // --- 新增功能：刷新当前页面 ---
                            //window.parent.location.reload();
                            // -----------------------------
                        }}
                    }}
                }} catch (err) {{
                    // 静默处理错误
                }}
            }}, 3000);
        }};

        // 遍历所有任务ID，为每一个启动轮询，并传入其详细信息
        jobIds.forEach(jobId => {{
            startPollingForJob(jobId, jobsData[jobId]);
        }});

    }})();
    </script>
    """

def inject_pollers_for_active_jobs():
    """
    【核心函数优化版】将所有活动任务的ID打包，一次性注入一个主轮询器。
    """
    # Only poll for real jobs, not mock jobs
    if "jobs" not in st.session_state:
        st.session_state.jobs = {}
    if "backend" not in st.session_state:
        st.session_state.backend = "http://localhost:8000"

    # Filter out mock jobs - only poll for real jobs
    real_jobs = {}
    if st.session_state.jobs:
        for job_id, job_info in st.session_state.jobs.items():
            # Skip mock jobs entirely
            if job_id.startswith("MOCK_JOB_"):
                continue
            # Skip mock jobs with is_mock flag
            is_mock = job_info.get("is_mock", False)
            if not is_mock:
                real_jobs[job_id] = job_info

    if not real_jobs:
        return

    # 将 Python 的 job_id 列表转换为 JSON 格式的字符串
    jobs_json_string = json.dumps(real_jobs)

    # 获取包含所有轮询逻辑的单个主脚本
    master_js_code = get_master_poller_html(jobs_json_string, st.session_state.backend)

    # 全局只调用这一次 components.html！
    components.html(master_js_code, height=0)

# import sys
# # --- START: 动态路径修改 ---
# # 这段代码会确保无论你从哪里运行脚本，都能正确找到 frontent 模块

# # 1. 获取当前文件 (utils.py) 所在的目录 (frontent/)
# current_dir = os.path.dirname(os.path.abspath(__file__))

# # 2. 获取 'frontent/' 的父目录 (也就是 'project/')
# project_root = os.path.dirname(current_dir)

# # 3. 如果 'project/' 目录不在Python的搜索路径中，就把它加进去
# if project_root not in sys.path:
#     sys.path.insert(0, project_root)

# # --- END: 动态路径修改 ---


# # 现在，因为 'project/' 目录已经在搜索路径里了，
# # 下面这个绝对导入就一定能成功
# from frontend.poller_component import poll_and_rerun

# def inject_pollers_for_active_jobs():
#     """
#     【最终版】使用自定义组件注入轮询器，并在完成后触发 st.rerun()。
#     此函数现在是对 poll_and_rerun 组件的一个封装。
#     """
#     if "jobs" not in st.session_state:
#         st.session_state.jobs = {}
#     if "backend" not in st.session_state:
#         # 确保有一个默认的后端URL
#         st.session_state.backend = "http://localhost:8000"

#     # 筛选出需要轮询的真实任务
#     real_jobs = {
#         job_id: job_info
#         for job_id, job_info in st.session_state.jobs.items()
#         if not job_id.startswith("MOCK_JOB_") and not job_info.get("is_mock", False)
#     }

#     if not real_jobs:
#         return

#     # 将任务字典转换为 JSON 字符串
#     jobs_json_string = json.dumps(real_jobs)

#     # 调用组件函数，它会处理所有前端逻辑和 rerun 触发
#     # 我们为 key 提供一个固定的字符串，以确保组件在不同页面间保持一致性
#     poll_and_rerun(jobs_json_string, st.session_state.backend, key="global_job_poller")


# utils.py

# ... (keep all your existing functions like initialize_session_state, etc.) ...

def get_all_jobs_for_selection():
    """
    Gets all jobs for selection in a dropdown, including mock and real tasks.
    Returns a dictionary mapping job_id to a user-friendly name.
    """
    all_jobs_for_selection = {}

    # 1. Add the mock task first as a baseline option
    if 'sample_data' in st.session_state and st.session_state.sample_data:
        assignment_stats = st.session_state.sample_data.get('assignment_stats')
        if assignment_stats:
            mock_job_id = "MOCK_JOB_001"
            all_jobs_for_selection[mock_job_id] = f"【模拟数据】{assignment_stats.assignment_name}"

    # 2. Add all real jobs from the session state
    if "jobs" in st.session_state and st.session_state.jobs:
        # Sort jobs by submission time, newest first
        sorted_job_ids = sorted(
            st.session_state.jobs.keys(),
            key=lambda jid: st.session_state.jobs[jid].get("submitted_at", "0"),
            reverse=True
        )

        for job_id in sorted_job_ids:
            if job_id.startswith("MOCK_JOB_"):
                continue

            task_info = st.session_state.jobs[job_id]
            job_name = task_info.get("name", f"任务-{job_id[:8]}")
            all_jobs_for_selection[job_id] = job_name

    return all_jobs_for_selection