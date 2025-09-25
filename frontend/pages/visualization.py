"""
可视化分析界面 (pages/visualization.py)

简化版本，专注于核心成绩展示功能
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import plotly.express as px
import plotly.graph_objects as go
import os
import json
from utils import *

# 导入自定义模块
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use the updated data loader that can handle AI grading data
from frontend_utils.data_loader import StudentScore, QuestionAnalysis, AssignmentStats, load_ai_grading_data, load_mock_data
from frontend_utils.chart_components import (
    create_score_distribution_chart, create_grade_pie_chart, create_question_accuracy_chart,
    create_knowledge_heatmap_chart, create_error_analysis_chart, create_difficulty_scatter_chart,
    create_question_heatmap_chart
)

# 页面配置
st.set_page_config(
    page_title="SmarTAI - 成绩展示",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

def init_session_state():
    """初始化会话状态"""
    # Check if we have a selected job for AI grading data
    if 'selected_job_id' in st.session_state and st.session_state.selected_job_id:
        # Load AI grading data
        with st.spinner("正在加载AI批改数据..."):
            ai_data = load_ai_grading_data(st.session_state.selected_job_id)
            if "error" not in ai_data:
                st.session_state.ai_grading_data = ai_data
            else:
                st.error(f"加载AI批改数据失败: {ai_data['error']}")
                # Fallback to mock data
                st.session_state.sample_data = load_mock_data()
    else:
        # Load mock data if no job is selected
        if 'sample_data' not in st.session_state:
            with st.spinner("加载数据中..."):
                st.session_state.sample_data = load_mock_data()

def render_header():
    """渲染页面头部"""
    col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
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
    
    with col:
        st.markdown("<h1 style='text-align: center; color: #000000;'>📈 成绩可视化分析</h1>", 
                   unsafe_allow_html=True)

    # with col8:
    #     # Export button
    #     if st.button("📤 导出数据", type="secondary"):
    #         st.info("导出功能将在后续版本中实现")

def render_filters(students: List[StudentScore], question_analysis: List[QuestionAnalysis]):
    """渲染筛选器"""
    st.markdown("## 🔍 数据筛选")
    
    # Create tabs for different filter categories
    tab1, tab2 = st.tabs(["学生筛选", "题目筛选"])
    
    with tab1:
        col1, col2 = st.columns(2)
        with col1:
            # 学号分段筛选
            student_ids = [s.student_id for s in students]
            if student_ids:
                min_id = min(student_ids)
                max_id = max(student_ids)
                selected_ids = st.multiselect("选择学号", student_ids, default=student_ids[:5])
        
        with col2:
            # 成绩等级筛选
            grade_levels = list(set([s.grade_level for s in students]))
            selected_grades = st.multiselect("选择成绩等级", grade_levels, default=grade_levels)
    
    with tab2:
        # 题目筛选
        if question_analysis:
            question_ids = [q.question_id for q in question_analysis]
            question_types = list(set([q.question_type for q in question_analysis]))
            
            col1, col2 = st.columns(2)
            with col1:
                selected_questions = st.multiselect("选择题目", question_ids)
            with col2:
                selected_question_types = st.multiselect("选择题型", question_types)
    
    # 知识点筛选功能暂未完善，先移除相关tab
    # Apply filters button
    if st.button("应用查看筛选结果"):
        st.success("筛选器已应用！")
    
    return students, question_analysis

def calculate_median_score(students: List[StudentScore]) -> float:
    """计算中位数成绩"""
    scores = [s.percentage for s in students]
    return np.median(scores) if scores else 0

def render_statistics_overview(students: List[StudentScore], assignment_stats: AssignmentStats):
    """渲染统计概览"""
    st.markdown("## 📊 成绩统计概览")
    
    # 计算统计数据
    if not students:  # 处理空数据情况
        st.warning("⚠️ 没有数据可显示")
        return
    
    scores = [s.percentage for s in students]
    avg_score = np.mean(scores)
    median_score = calculate_median_score(students)
    max_score = np.max(scores)
    min_score = np.min(scores)
    std_score = np.std(scores)
    pass_rate = len([s for s in scores if s >= 60]) / len(scores) * 100 if scores else 0
    excellence_rate = len([s for s in scores if s >= 85]) / len(scores) * 100 if scores else 0
    
    # 显示统计卡片
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    
    with col1:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{len(students)}</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">提交人数</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{avg_score:.1f}</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">平均分</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{median_score:.1f}</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">中位数</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{max_score:.1f}</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">最高分</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col5:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{min_score:.1f}</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">最低分</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col6:
        st.markdown(f"""
        <div style="background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); text-align: center; border-top: 4px solid #1E3A8A;">
            <div style="font-size: 2.5rem; font-weight: 700; color: #1E3A8A; margin-bottom: 0.25rem;">{pass_rate:.1f}%</div>
            <div style="font-size: 0.875rem; color: #64748B; text-transform: uppercase; font-weight: 600;">及格率</div>
        </div>
        """, unsafe_allow_html=True)

def render_student_table(students: List[StudentScore]):
    """渲染学生表格"""
    st.markdown("## 📋 学生成绩列表")
    
    if not students:
        st.warning("⚠️ 没有学生数据")
        return
    
    # 准备表格数据
    data = []
    for student in students:
        # Determine color based on grade level
        if student.grade_level == "优秀":
            grade_color = "#10B981"  # green
        elif student.grade_level == "良好":
            grade_color = "#3B82F6"  # blue
        elif student.grade_level == "中等":
            grade_color = "#2E8B57"  # teal
        elif student.grade_level == "及格":
            grade_color = "#F59E0B"  # orange
        else:  # 不及格
            grade_color = "#EF4444"  # red
            
        # Apply color to grade level
        colored_grade = f"<span style='color: {grade_color}; font-weight: bold;'>{student.grade_level}</span>"
        
        data.append({
            "学号": student.student_id,
            "姓名": student.student_name,
            "总分": f"{student.total_score:.1f}/{student.max_score}",
            "百分比": f"{student.percentage:.1f}%",
            "等级": colored_grade,
            "提交时间": student.submit_time.strftime('%Y-%m-%d %H:%M'),
            "置信度": f"{student.confidence_score:.1%}",
            "需复核": "是" if student.need_review else "否"
        })
    
    df = pd.DataFrame(data)
    
    # 显示表格 with colored grade levels
    st.write(df.to_html(escape=False, index=False), unsafe_allow_html=True)

def render_charts(students: List[StudentScore], question_analysis: List[QuestionAnalysis]):
    """渲染图表"""
    st.markdown("## 📈 成绩分布图表")
    
    if not students:
        st.warning("⚠️ 没有数据可显示")
        return
    
    try:
        # Create tabs for different chart categories
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["成绩分布", "题目分析", "错误分析", "题目热力图", "知识点掌握", "教学建议"])
        
        with tab1:
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("### 成绩分布直方图")
                fig1 = create_score_distribution_chart(students)
                st.plotly_chart(fig1, use_container_width=True)
            
            with col2:
                st.markdown("### 成绩等级分布")
                fig2 = create_grade_pie_chart(students)
                st.plotly_chart(fig2, use_container_width=True)
        
        with tab2:
            if question_analysis:
                col1, col2 = st.columns(2)
                
                with col1:
                    st.markdown("### 各题目正确率分析")
                    fig3 = create_question_accuracy_chart(question_analysis)
                    st.plotly_chart(fig3, use_container_width=True)
                
                with col2:
                    st.markdown("### 题目难度 vs 正确率")
                    fig4 = create_difficulty_scatter_chart(question_analysis)
                    st.plotly_chart(fig4, use_container_width=True)
            else:
                st.info("暂无题目分析数据")
        
        with tab3:
            if question_analysis:
                st.markdown("### 易错点统计 (Top 10)")
                fig6 = create_error_analysis_chart(question_analysis)
                st.plotly_chart(fig6, use_container_width=True)
            else:
                st.info("暂无错误分析数据")
                
        with tab4:
            if question_analysis:
                st.markdown("### 题目分析热力图")
                fig7 = create_question_heatmap_chart(question_analysis)
                st.plotly_chart(fig7, use_container_width=True)
            else:
                st.info("暂无题目分析数据")
                
        with tab5:
            # 知识点掌握功能移到最后一栏
            st.info("知识点掌握功能正在完善中，敬请期待...")
            # if question_analysis:
            #     st.markdown("### 知识点掌握度热力图")
            #     fig5 = create_knowledge_heatmap_chart(question_analysis)
            #     st.plotly_chart(fig5, use_container_width=True)
            # else:
            #     st.info("暂无知识点分析数据")
            
        with tab6:
            st.info("教学建议功能正在开发中...")
                
    except Exception as e:
        st.error(f"生成图表时出错: {str(e)}")

def render_weakness_analysis(question_analysis: List[QuestionAnalysis]):
    """渲染教学薄弱环节分析"""
    st.markdown("## ⚠️ 教学薄弱环节识别")
    
    if not question_analysis:
        st.info("暂无题目分析数据")
        return
    
    # Find questions with low correct rates (high error rates)
    low_correct_questions = [q for q in question_analysis if q.correct_rate < 0.6]
    
    if low_correct_questions:
        # Sort by correct rate (ascending)
        low_correct_questions.sort(key=lambda x: x.correct_rate)
        
        st.markdown("### 易错题排序")
        for i, question in enumerate(low_correct_questions[:5], 1):  # Top 5
            error_color = "#EF4444" if question.correct_rate < 0.4 else "#F59E0B"
            st.markdown(f"""
            <div style="background: white; padding: 1rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 0.5rem 0; border-left: 4px solid {error_color};">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h4 style="color: #1E3A8A; margin: 0;">{i}. 题目 {question.question_id} ({question.question_type})</h4>
                    <span style="font-size: 1.2rem; font-weight: bold; color: {error_color};">{question.correct_rate:.1%}</span>
                </div>
                <p style="margin: 0.5rem 0 0 0; color: #64748B;">知识点: {', '.join(question.knowledge_points[:3])}</p>
                <p style="margin: 0.5rem 0 0 0; color: #64748B;">常见错误: {', '.join(question.common_errors[:3])}</p>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.success("✅ 暂未发现明显的教学薄弱环节")
    
    # 知识点功能暂未完善，先注释掉展示
    # st.markdown("### 知识点掌握情况")
    # st.info("知识点掌握功能正在完善中，敬请期待...")

def render_export_section():
    """渲染导出和分享功能"""
    st.markdown("## 📤 数据导出与分享")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("### 导出到飞书多维表格")
        st.info("此功能将在后续版本中实现")
        # st.button("📤 导出当前视图", disabled=True)
        
    with col2:
        st.markdown("### 生成仪表盘快照")
        st.info("此功能将在后续版本中实现")
        # st.button("🔗 生成分享链接", disabled=True)
    
    with col3:
        st.markdown("### 生成PDF报告")
        if st.button("📄 生成PDF报告"):
            try:
                # Import PDF generator
                from frontend_utils.pdf_generator import generate_assignment_report
                
                # Get data for the report
                if 'ai_grading_data' in st.session_state and st.session_state.ai_grading_data:
                    data = st.session_state.ai_grading_data
                elif 'sample_data' in st.session_state and st.session_state.sample_data:
                    data = st.session_state.sample_data
                else:
                    # Load mock data as fallback
                    from frontend_utils.data_loader import load_mock_data
                    data = load_mock_data()
                
                students = data.get('student_scores', [])
                assignment_stats = data.get('assignment_stats', None)
                question_analysis = data.get('question_analysis', [])
                
                if assignment_stats and students:
                    with st.spinner("正在生成PDF报告..."):
                        # Generate PDF report
                        pdf_path = generate_assignment_report(assignment_stats, students, question_analysis)
                        
                        # Provide download link
                        with open(pdf_path, "rb") as file:
                            st.download_button(
                                label="📥 下载PDF报告",
                                data=file,
                                file_name=f"{assignment_stats.assignment_name}_可视化报告.pdf",
                                mime="application/pdf",
                                key="download_pdf_viz"
                            )
                        st.success("PDF报告已生成！点击上方按钮下载。")
                else:
                    st.warning("无法生成报告：缺少必要的数据。")
            except Exception as e:
                st.error(f"生成PDF报告时出错: {str(e)}")

def main():
    """主函数"""
    # 初始化
    init_session_state()
    
    # 渲染页面
    render_header()
    
    # --- 改动 1: 替换旧的数据加载逻辑 ---
    # 旧的 init_session_state 和数据获取逻辑被以下更强大的选择器取代。
    selectable_jobs = get_all_jobs_for_selection()

    if not selectable_jobs:
        st.warning("当前没有批改任务记录可供分析。")
        st.stop()

    job_ids = list(selectable_jobs.keys())
    default_index = 0

    # --- 改动 2: 实现与 score_report.py 一致的智能默认选择 ---
    # 优先级 1: 从 history.py 跳转而来
    if "selected_job_from_history" in st.session_state:
        job_id_from_history = st.session_state.selected_job_from_history
        if job_id_from_history in job_ids:
            default_index = job_ids.index(job_id_from_history)
        del st.session_state.selected_job_from_history
    
    # 优先级 2: 使用在其他页面已选中的全局任务ID
    elif "selected_job_id" in st.session_state and st.session_state.selected_job_id in job_ids:
        default_index = job_ids.index(st.session_state.selected_job_id)

    # --- 改动 3: 创建下拉选择框 ---
    def on_selection_change():
        """回调函数：当用户手动选择后，更新全局的任务ID"""
        st.session_state.selected_job_id = st.session_state.viz_job_selector

    selected_job = st.selectbox(
        "选择要进行可视化分析的批改任务",
        options=job_ids,
        format_func=lambda jid: selectable_jobs.get(jid, jid),
        index=default_index,
        key="viz_job_selector", # 使用唯一的 key
        on_change=on_selection_change
    )
    
    # 实时更新全局选择ID，确保页面内状态一致
    st.session_state.selected_job_id = selected_job
    st.markdown("---")

    # --- 改动 4: 根据下拉框的选择，加载对应的数据 ---
    data_to_display = None
    if selected_job.startswith("MOCK_JOB"):
        # 如果是模拟任务，直接从 session_state 加载模拟数据
        data_to_display = st.session_state.get('sample_data', load_mock_data())
    else:
        # 如果是真实任务，从后端API加载数据
        with st.spinner("正在加载AI批改数据..."):
            ai_data = load_ai_grading_data(selected_job)
            if "error" not in ai_data:
                data_to_display = ai_data
            else:
                st.error(f"加载AI批改数据失败: {ai_data['error']}")
                st.info("将显示模拟数据作为备用。")
                data_to_display = st.session_state.get('sample_data', load_mock_data())
    
    if not data_to_display:
        st.warning("无法加载所选任务的数据。")
        st.stop()
        
    # --- 改动 5: 使用新加载的数据驱动页面渲染 ---
    # 旧代码是直接从 st.session_state.ai_grading_data 或 sample_data 中获取数据,
    # 现在我们统一从 data_to_display 变量中获取。
    # 后续的渲染函数完全不需要修改。
    students = data_to_display.get('student_scores', [])
    assignment_stats = data_to_display.get('assignment_stats', None)
    question_analysis = data_to_display.get('question_analysis', [])
    
    # 如果 assignment_stats 不存在，创建一个默认的（此逻辑与您原代码一致）
    if not assignment_stats and students:
         assignment_stats = AssignmentStats(
            assignment_id="DEFAULT",
            assignment_name="示例作业",
            total_students=len(students),
            submitted_count=len(students),
            avg_score=np.mean([s.percentage for s in students]) if students else 0,
            max_score=max([s.percentage for s in students]) if students else 0,
            min_score=min([s.percentage for s in students]) if students else 0,
            std_score=np.std([s.percentage for s in students]) if students else 0,
            pass_rate=(len([s for s in students if s.percentage >= 60]) / len(students) * 100) if students else 0,
            question_count=len(question_analysis),
            create_time=datetime.now()
        )

    # 渲染统计概览
    if assignment_stats:
        render_statistics_overview(students, assignment_stats)
    
    st.markdown("---")
    
    # 渲染学生表格
    render_student_table(students)
    
    st.markdown("---")
    
    # 渲染图表
    render_charts(students, question_analysis)
    
    st.markdown("---")
    
    # 渲染教学薄弱环节分析
    render_weakness_analysis(question_analysis)
    
    st.markdown("---")
    
    # 渲染导出和分享功能
    render_export_section()

    inject_pollers_for_active_jobs()

if __name__ == "__main__":
    main()