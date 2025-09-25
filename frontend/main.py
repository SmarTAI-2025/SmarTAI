"""
SmarTAI项目 - 主应用入口文件

智能评估平台的主界面，提供导航和核心功能入口
"""

import streamlit as st
import sys
import os
import json
from datetime import datetime
from typing import Dict, Any

# 添加项目路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import from utils.py (the file, not the folder)
from utils import *
# Import from frontend_utils (the folder we renamed)
from frontend_utils.data_loader import load_ai_grading_data, StudentScore, QuestionAnalysis, AssignmentStats
from frontend_utils.chart_components import create_score_distribution_chart, create_grade_pie_chart

# 页面配置
st.set_page_config(
    page_title="SmarTAI - 智能评估平台",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="collapsed"
)

def load_mock_data():
    """Load mock data for testing when real data is not available"""
    try:
        # Try to load from root directory first (where the file actually is)
        mock_data_path = os.path.join(os.path.dirname(__file__), "..", "mock_data.json")
        if not os.path.exists(mock_data_path):
            # Fallback to frontend directory
            mock_data_path = os.path.join(os.path.dirname(__file__), "mock_data.json")
        
        with open(mock_data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Convert string dates back to datetime objects
        for student in data["student_scores"]:
            if isinstance(student["submit_time"], str):
                student["submit_time"] = datetime.fromisoformat(student["submit_time"])
        
        if isinstance(data["assignment_stats"]["create_time"], str):
            data["assignment_stats"]["create_time"] = datetime.fromisoformat(data["assignment_stats"]["create_time"])
        
        # Convert to proper dataclass objects
        student_scores = []
        for student_data in data["student_scores"]:
            student_scores.append(StudentScore(**student_data))
        
        question_analysis = []
        for question_data in data["question_analysis"]:
            question_analysis.append(QuestionAnalysis(**question_data))
        
        assignment_stats = AssignmentStats(**data["assignment_stats"])
        
        return {
            "student_scores": student_scores,
            "question_analysis": question_analysis,
            "assignment_stats": assignment_stats
        }
    except Exception as e:
        st.error(f"Failed to load mock data: {str(e)}")
        # return create_default_data()

def init_session_state():
    """初始化会话状态"""
    # Initialize session state from utils.py
    initialize_session_state()
    
    # Set logged in state
    st.session_state.logged_in = True
    
    # Initialize sample data or AI grading data
    if 'sample_data' not in st.session_state:
        with st.spinner("初始化系统数据..."):
            # Try to load AI grading data if a job is selected
            if 'selected_job_id' in st.session_state:
                ai_data = load_ai_grading_data(st.session_state.selected_job_id)
                if "error" not in ai_data:
                    st.session_state.sample_data = ai_data
                else:
                    # Load mock data if AI data loading fails
                    st.session_state.sample_data = load_mock_data()
            else:
                # Load mock data when no job is selected (before any grading)
                st.session_state.sample_data = load_mock_data()
    
    # Don't load mock jobs from file, use static mock data in history pages instead
    # This prevents the continuous submission of mock grading tasks
    
    if 'user_info' not in st.session_state:
        st.session_state.user_info = {
            'name': '张老师',
            'role': '任课教师',
            'department': '计算机科学与技术学院'
        }

def render_hero_section():
    """渲染主题部分"""
    st.markdown("""
    <div class="hero-section">
        <h1 style="text-align: center; color: #000000; margin-bottom: 1rem; font-weight: 700;">🎓 SmarTAI</h1>
        <h2 style="text-align: center; color: #000000; margin-bottom: 0.5rem; opacity: 0.9;">智能作业评估平台</h2>
        <h4 style='text-align: center; color: #000000;'>高效、智能、全面——您的自动化教学助理。</h4>
        <p style="font-size: 1.125rem; opacity: 0.8; max-width: 600px; margin: 0 auto;">
            基于人工智能的理工科教育评估系统提供智能评分、深度分析和可视化报告
        </p>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("---")


def render_user_welcome():
    """渲染用户欢迎信息"""
    user_info = st.session_state.user_info
    col1, col2,col3 = st.columns([35,35,15])
    
    with col1:
        # 显示登录用户信息
        username = st.session_state.get('username', user_info['name'])
        st.markdown(f"""
        ### 👋 欢迎回来，{username}！
        **{user_info['role']}** | {user_info['department']}
        """)
    
    with col2:
        current_time = datetime.now()
        st.markdown(f"""
        ### 📅 今日信息
        **日期:** {current_time.strftime('%Y年%m月%d日 ')}
        **时间:** {current_time.strftime('%H:%M')}
        """, unsafe_allow_html=True)
    
    with col3:
        if st.button("🔄 刷新数据", use_container_width=False):
            # Refresh data based on selected job or default data
            if 'selected_job_id' in st.session_state:
                ai_data = load_ai_grading_data(st.session_state.selected_job_id)
                if "error" not in ai_data:
                    st.session_state.sample_data = ai_data
                else:
                    st.session_state.sample_data = load_mock_data()
            else:
                st.session_state.sample_data = load_mock_data()
            st.success("数据已刷新！")
            st.rerun()
        
        if st.button("🚪 退出登录", use_container_width=False, type="secondary"):
            # 清除登录状态
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.success("已退出登录")
            st.switch_page("pages/login.py")

def render_statistics_overview():
    """渲染统计概览"""
    st.markdown("## 📊 今日概览")
    
    # 获取统计数据
    data = st.session_state.sample_data
    
    # Check if data contains required keys
    if 'student_scores' not in data:
        st.error("数据加载失败：缺少学生分数信息")
        return
        
    students = data['student_scores']
    
    # Handle case where assignment_stats might be missing
    if 'assignment_stats' in data:
        assignment_stats = data['assignment_stats']
        total_students = assignment_stats.total_students
        avg_score = assignment_stats.avg_score
        pass_rate = assignment_stats.pass_rate
        need_review = len([s for s in students if s.need_review])
    else:
        # Calculate stats from student data if assignment_stats is missing
        total_students = len(students)
        avg_score = sum(s.percentage for s in students) / len(students) if students else 0
        pass_rate = len([s for s in students if s.percentage >= 60]) / len(students) * 100 if students else 0
        need_review = len([s for s in students if s.need_review])
    
    # 显示统计卡片
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""
        <div class="stats-card">
            <div class="stats-number">{total_students}</div>
            <div class="stats-label">学生总数</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown(f"""
        <div class="stats-card">
            <div class="stats-number">{avg_score:.1f}%</div>
            <div class="stats-label">平均成绩</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown(f"""
        <div class="stats-card">
            <div class="stats-number">{pass_rate:.1f}%</div>
            <div class="stats-label">及格率</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        st.markdown(f"""
        <div class="stats-card">
            <div class="stats-number">{need_review}</div>
            <div class="stats-label">待复核</div>
        </div>
        """, unsafe_allow_html=True)

def render_feature_cards():
    """渲染功能特性卡片"""
    st.markdown("## 🚀 核心功能")
    
    col1, col2 = st.columns(2)
    col3, col4 = st.columns(2)
    
    with col1:
        st.markdown("""
        <div class="feature-card">
            <div class="feature-icon">📊</div>
            <div class="feature-title">作业评分报告</div>
            <div class="feature-description">
                查看学生作业详细评分结果，支持人工修改和批量操作。
                提供置信度分析和复核建议。
            </div>
            <div class="feature-card-buttons">
        """, unsafe_allow_html=True)
        
        if st.button("📊 查看评分报告", use_container_width=True, type="primary", key="report_button_1"):
            # Don't clear the selected job ID, keep it so the score report can load the data
            # if 'selected_job_id' in st.session_state:
            #     del st.session_state.selected_job_id
            st.switch_page("pages/score_report.py")

        st.markdown("""
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown("""
        <div class="feature-card">
            <div class="feature-icon">📈</div>
            <div class="feature-title">成绩可视化分析</div>
            <div class="feature-description">
                深度分析学生成绩表现和题目质量，生成交互式图表和统计报告。
                支持多维度数据分析。
            </div>
            <div class="feature-card-buttons">
        """, unsafe_allow_html=True)
        
        if st.button("📈 查看可视化分析", use_container_width=True, type="primary", key="viz_button_2"):
            # Don't clear the selected job ID, keep it so the visualization can load the data
            # if 'selected_job_id' in st.session_state:
            #     del st.session_state.selected_job_id
            st.switch_page("pages/visualization.py")

        st.markdown("""
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown("""
        <div class="feature-card">
            <div class="feature-icon">🕒</div>
            <div class="feature-title">历史批改记录</div>
            <div class="feature-description">
                查看历史批改记录，支持暂存功能。可以预览、编辑暂存记录，
                查看已完成批改的作业详情。
            </div>
            <div class="feature-card-buttons">
        """, unsafe_allow_html=True)
        
        if st.button("🕒 查看历史记录", use_container_width=True, type="primary", key="history_button_3"):
            st.switch_page("pages/history.py")

        st.markdown("""
            </div>
        </div>
        """, unsafe_allow_html=True)

    with col4:
        st.markdown("""
        <div class="feature-card">
            <div class="feature-icon">📚</div>
            <div class="feature-title">知识数据库管理</div>
            <div class="feature-description">
                知识库管理，支持查看、新建、修改、删除知识库及其中文件，
                查看已存在的知识库详情。
            </div>
            <div class="feature-card-buttons">
        """, unsafe_allow_html=True)
        
        if st.button("📚 查看历史记录", use_container_width=True, type="primary", key="history_button_4"):
            st.switch_page("pages/knowledge_base.py")

        st.markdown("""
            </div>
        </div>
        """, unsafe_allow_html=True)

def render_upload_section():
    """渲染上传功能区域"""
    st.markdown("## 📤 作业上传")
    
    st.markdown("""
    <div style="background: white; padding: 2rem; border-radius: 15px; box-shadow: 0 8px 25px rgba(0,0,0,0.1); border-top: 4px solid #1E3A8A;">
        <h3 style="color: #1E3A8A; margin-top: 0;">开始新的作业批改流程</h3>
        <p>上传题目和学生作业文件，启动AI智能批改流程。</p>
    </div>
    """, unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 📝 第一步：上传题目文件")
        st.markdown("上传包含题目的PDF或Word文档")
        if st.button("📁 上传题目文件", use_container_width=True, type="primary"):
            st.switch_page("pages/prob_upload.py")

    with col2:
        st.markdown("### 📄 第二步：上传学生作业")
        st.markdown("上传学生提交的作业文件")
        if st.button("📁 上传学生作业", use_container_width=True, type="primary"):
            st.switch_page("pages/hw_upload.py")

def render_quick_preview():
    """渲染快速预览"""
    st.markdown("## 👀 快速预览")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 📊 成绩分布")
        try:
            students = st.session_state.sample_data['student_scores']
            fig = create_score_distribution_chart(students)
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"生成图表时出错: {str(e)}")
    
    with col2:
        st.markdown("### 🏆 成绩等级")
        try:
            students = st.session_state.sample_data['student_scores']
            fig = create_grade_pie_chart(students)
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"生成图表时出错: {str(e)}")

# def render_quick_actions():
#     """渲染快速操作"""
#     st.markdown("""
#     <div class="quick-access">
#         <h3 style="color: #1E3A8A; margin-bottom: 1.5rem;">⚡ 快速操作</h3>
#     """, unsafe_allow_html=True)
    
#     col1, col2, col3, col4, col5, col6 = st.columns(6)
    
#     with col1:
#         if st.button("📋 最新作业", use_container_width=True):
#             st.info("🔄 跳转到最新作业评分...")
    
#     with col2:
#         if st.button("⚠️ 待复核列表", use_container_width=True):
#             st.info("📝 显示需要复核的作业...")
    
#     with col3:
#         if st.button("📈 生成报告", use_container_width=True):
#             with st.spinner("生成综合分析报告中..."):
#                 import time
#                 time.sleep(2)
#             st.success("✅ 综合分析报告已生成！")
    
#     with col4:
#         if st.button("📚 知识库管理", use_container_width=True):
#             st.switch_page("pages/knowledge_base.py")
    
#     with col5:
#         if st.button("📊 批改结果", use_container_width=True):
#             st.switch_page("pages/grade_results.py")
    
#     with col6:
#         if st.button("⚙️ 系统设置", use_container_width=True):
#             st.info("🔧 打开系统设置界面...")
    
#     st.markdown("</div>", unsafe_allow_html=True)

def render_recent_activities():
    """渲染最近活动"""
    st.markdown("## 🕐 最近活动")
    
    activities = [
        {
            "time": "2小时前",
            "action": "批量导出PDF报告",
            "details": "导出了45名学生的评分报告",
            "status": "完成"
        },
        {
            "time": "5小时前",
            "action": "复核低置信度答案",
            "details": "复核了8道置信度低于70%的题目",
            "status": "完成"
        },
        {
            "time": "1天前",
            "action": "生成可视化分析",
            "details": "为数据结构课程生成了综合分析报告",
            "status": "完成"
        },
        {
            "time": "2天前",
            "action": "上传新作业",
            "details": "上传了期中考试试卷，等待AI评分",
            "status": "处理中"
        }
    ]
    
    for activity in activities:
        status_color = "#10B981" if activity["status"] == "完成" else "#F59E0B"
        st.markdown(f"""
        <div style="background: white; padding: 1rem; border-radius: 8px; margin: 0.5rem 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <strong style="color: #1E3A8A;">{activity['action']}</strong><br>
                    <span style="color: #64748B; font-size: 0.875rem;">{activity['details']}</span>
                </div>
                <div style="text-align: right;">
                    <span style="color: {status_color}; font-weight: 600;">{activity['status']}</span><br>
                    <span style="color: #64748B; font-size: 0.75rem;">{activity['time']}</span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

def render_footer():
    """渲染页脚"""
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("""
        ### 📞 技术支持
        **邮箱:** smartai2025@126.com
        """)
    
    with col2: #TODO: 添加帮助链接
        st.markdown("""
        ### 📚 使用帮助
        - 用户手册 (敬请期待)
        - 常见问题 (敬请期待)
        """)
    
    with col3:
        st.markdown("""
        ### ℹ️ 系统信息
        **版本:** v1.0.0
        **最后更新:** 2025-09-30
        """)

def render_dashboard():
    """渲染主界面内容（登录后显示）"""
    # 加载CSS和初始化
    load_custom_css()
    init_session_state()
    
    # # Inject pollers for active jobs
    # inject_pollers_for_active_jobs()
    
    # 渲染页面各个部分
    render_hero_section()
    render_user_welcome()
    
    st.markdown("---")
    render_statistics_overview()
    
    st.markdown("---")
    render_upload_section()
    
    st.markdown("---")
    render_feature_cards()
    
    st.markdown("---")
    render_quick_preview()
    
    # st.markdown("---")
    # render_quick_actions()
    
    st.markdown("---")
    render_recent_activities()
    
    render_footer()

    inject_pollers_for_active_jobs()

def main():
    """主函数 - 应用入口点"""
    # 检查登录状态
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
    
    # 如果未登录，重定向到登录页面
    if not st.session_state.logged_in:
        st.switch_page("pages/login.py")
    else:
        # 如果已登录，显示主界面内容
        render_dashboard()

if __name__ == "__main__":
    main()