"""
图表组件工具模块

包含各种可视化图表的生成函数，支持Plotly和Altair图表
"""

import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import streamlit as st
from typing import List, Dict, Any, Optional
import altair as alt

from .data_loader import StudentScore, QuestionAnalysis, AssignmentStats

class ChartComponents:
    """图表组件类"""
    
    def __init__(self):
        """初始化图表配置"""
        # 配色方案
        self.colors = {
            'primary': '#1E3A8A',      # 深蓝色
            'secondary': '#F59E0B',    # 橙色
            'success': '#10B981',      # 绿色
            'warning': '#F59E0B',      # 橙色
            'danger': '#EF4444',       # 红色
            'info': '#3B82F6',         # 蓝色
            'teal': '#2E8B57',         # 海蓝绿
            'skyblue': '#87CEEB'       # 天空蓝
        }
        
        # 图表默认配置
        self.default_layout = {
            'font': {'family': 'Noto Sans SC, sans-serif', 'size': 12},
            'plot_bgcolor': 'white',
            'paper_bgcolor': 'white',
            'margin': {'l': 60, 'r': 60, 't': 60, 'b': 60}
        }
    
    def create_score_distribution_histogram(self, student_scores: List[StudentScore]) -> go.Figure:
        """创建成绩分布直方图"""
        scores = [score.percentage for score in student_scores]
        
        fig = go.Figure()
        
        fig.add_trace(go.Histogram(
            x=scores,
            nbinsx=10,
            marker_color=self.colors['primary'],
            opacity=0.8,
            name='学生人数'
        ))
        
        # 添加平均分线
        avg_score = np.mean(scores)
        fig.add_vline(
            x=avg_score,
            line_dash="dash",
            line_color=self.colors['secondary'],
            annotation_text=f"平均分: {avg_score:.1f}%"
        )
        
        # 添加中位数线
        median_score = np.median(scores)
        fig.add_vline(
            x=median_score,
            line_dash="dot",
            line_color=self.colors['info'],
            annotation_text=f"中位数: {median_score:.1f}%"
        )
        
        fig.update_layout(
            title="成绩分布直方图",
            xaxis_title="成绩百分比 (%)",
            yaxis_title="学生人数",
            **self.default_layout
        )
        
        return fig
    
    def create_grade_distribution_pie(self, student_scores: List[StudentScore]) -> go.Figure:
        """创建成绩等级分布饼图"""
        grade_counts = {}
        for score in student_scores:
            grade = score.grade_level
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
        
        labels = list(grade_counts.keys())
        values = list(grade_counts.values())
        
        # Define colors for each grade level
        grade_colors = {
            '优秀': self.colors['success'],    # Green for excellent
            '良好': self.colors['info'],       # Blue for good
            '中等': self.colors['teal'],       # Teal for average
            '及格': self.colors['secondary'],  # Orange for passing
            '不及格': self.colors['danger']    # Red for failing
        }
        
        # Map colors to labels in the same order
        colors = [grade_colors.get(label, self.colors['primary']) for label in labels]
        
        fig = go.Figure(data=[go.Pie(
            labels=labels,
            values=values,
            hole=0.4,
            marker_colors=colors
        )])
        
        fig.update_layout(
            title="成绩等级分布",
            **self.default_layout
        )
        
        return fig
    
    def create_question_accuracy_bar(self, question_analysis: List[QuestionAnalysis]) -> go.Figure:
        """创建题目正确率柱状图"""
        questions = [f"Q{i+1}" for i in range(len(question_analysis))]
        accuracy_rates = [q.correct_rate * 100 for q in question_analysis]
        question_types = [q.question_type for q in question_analysis]
        
        # 根据题目类型设置颜色
        type_colors = {
            '概念题': self.colors['primary'],
            '计算题': self.colors['success'],
            '证明题': self.colors['warning'],
            '编程题': self.colors['info']
        }
        
        colors = [type_colors.get(qt, self.colors['primary']) for qt in question_types]
        
        fig = go.Figure()
        
        fig.add_trace(go.Bar(
            x=questions,
            y=accuracy_rates,
            marker_color=colors,
            text=[f"{rate:.1f}%" for rate in accuracy_rates],
            textposition='outside',
            name='正确率'
        ))
        
        # 添加及格线
        fig.add_hline(
            y=60,
            line_dash="dash",
            line_color=self.colors['danger'],
            annotation_text="及格线 (60%)"
        )
        
        fig.update_layout(
            title="各题目正确率分析",
            xaxis_title="题目编号",
            yaxis_title="正确率 (%)",
            **self.default_layout
        )
        
        return fig
    
    def create_knowledge_heatmap(self, question_analysis: List[QuestionAnalysis]) -> go.Figure:
        """创建知识点掌握度热力图"""
        # 统计知识点掌握情况
        knowledge_stats = {}
        for qa in question_analysis:
            for kp in qa.knowledge_points:
                if kp not in knowledge_stats:
                    knowledge_stats[kp] = []
                knowledge_stats[kp].append(qa.correct_rate)
        
        # 计算平均掌握度
        knowledge_mastery = {kp: np.mean(rates) for kp, rates in knowledge_stats.items()}
        
        # 创建矩阵数据 (为了热力图效果，创建一个网格)
        knowledge_points = list(knowledge_mastery.keys())
        mastery_values = list(knowledge_mastery.values())
        
        # 将数据重塑为矩阵形式
        n_cols = 3
        n_rows = (len(knowledge_points) + n_cols - 1) // n_cols
        
        # 填充数据到矩阵
        matrix = np.zeros((n_rows, n_cols))
        labels = [['' for _ in range(n_cols)] for _ in range(n_rows)]
        
        for i, (kp, mastery) in enumerate(knowledge_mastery.items()):
            row, col = divmod(i, n_cols)
            if row < n_rows:
                matrix[row][col] = mastery
                labels[row][col] = kp
        
        fig = go.Figure(data=go.Heatmap(
            z=matrix,
            text=labels,
            texttemplate="%{text}<br>%{z:.1%}",
            textfont={"size": 10},
            colorscale='RdYlGn',
            reversescale=False,
            showscale=True,
            colorbar=dict(title="掌握度")
        ))
        
        fig.update_layout(
            title="知识点掌握度热力图",
            xaxis_title="",
            yaxis_title="",
            xaxis=dict(showticklabels=False),
            yaxis=dict(showticklabels=False),
            **self.default_layout
        )
        
        return fig
    
    def create_student_radar_chart(self, student_score: StudentScore) -> go.Figure:
        """创建学生个人能力雷达图"""
        # 按题目类型统计能力
        type_scores = {}
        type_counts = {}
        
        for q in student_score.questions:
            qtype = q['question_type']
            score_rate = q['score'] / q['max_score']
            
            if qtype not in type_scores:
                type_scores[qtype] = 0
                type_counts[qtype] = 0
            
            type_scores[qtype] += score_rate
            type_counts[qtype] += 1
        
        # 计算各类型平均得分
        categories = []
        values = []
        type_names = {
            '概念题': '概念理解',
            '计算题': '计算能力',
            '证明题': '证明推理',
            '编程题': '编程实现'
        }
        
        for qtype, total_score in type_scores.items():
            avg_score = (total_score / type_counts[qtype]) * 100
            categories.append(type_names.get(qtype, qtype))
            values.append(avg_score)
        
        # 闭合雷达图
        categories.append(categories[0])
        values.append(values[0])
        
        fig = go.Figure()
        
        fig.add_trace(go.Scatterpolar(
            r=values,
            theta=categories,
            fill='toself',
            name=student_score.student_name,
            line_color=self.colors['primary'],
            fillcolor=self.colors['primary'],
            opacity=0.3
        ))
        
        fig.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[0, 100]
                )
            ),
            showlegend=True,
            title=f"{student_score.student_name} - 能力雷达图",
            **self.default_layout
        )
        
        return fig
    
    def create_error_analysis_bar(self, question_analysis: List[QuestionAnalysis]) -> go.Figure:
        """创建错误分析柱状图"""
        # 统计所有错误类型
        error_counts = {}
        for qa in question_analysis:
            for error in qa.common_errors:
                error_counts[error] = error_counts.get(error, 0) + 1
        
        # 按频次排序，取前10
        sorted_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        
        errors = [item[0] for item in sorted_errors]
        counts = [item[1] for item in sorted_errors]
        
        fig = go.Figure()
        
        fig.add_trace(go.Bar(
            x=counts,
            y=errors,
            orientation='h',
            marker_color=self.colors['danger'],
            text=counts,
            textposition='outside'
        ))
        
        fig.update_layout(
            title="易错点统计 (Top 10)",
            xaxis_title="出现次数",
            yaxis_title="错误类型",
            **self.default_layout
        )
        
        return fig
    
    def create_score_trend_line(self, student_scores: List[StudentScore]) -> go.Figure:
        """创建成绩趋势线图（模拟历史数据）"""
        # 为演示目的，生成模拟的历史趋势数据
        dates = pd.date_range(start='2024-01-01', end='2024-09-01', freq='ME')
        
        # 模拟班级平均分趋势
        base_score = 75
        trends = []
        for i, date in enumerate(dates):
            # 添加一些随机波动和整体上升趋势
            trend_score = base_score + i * 2 + np.random.normal(0, 5)
            trends.append(min(100, max(0, trend_score)))
        
        fig = go.Figure()
        
        fig.add_trace(go.Scatter(
            x=dates,
            y=trends,
            mode='lines+markers',
            name='班级平均分',
            line=dict(color=self.colors['primary'], width=3),
            marker=dict(size=8)
        ))
        
        # 添加当前成绩点
        current_avg = np.mean([s.percentage for s in student_scores])
        fig.add_trace(go.Scatter(
            x=[dates[-1] + pd.Timedelta(days=30)],
            y=[current_avg],
            mode='markers',
            name='当前平均分',
            marker=dict(size=12, color=self.colors['secondary'])
        ))
        
        fig.update_layout(
            title="班级成绩趋势分析",
            xaxis_title="时间",
            yaxis_title="平均分 (%)",
            **self.default_layout
        )
        
        return fig
    
    def create_difficulty_vs_accuracy_scatter(self, question_analysis: List[QuestionAnalysis]) -> go.Figure:
        """创建难度vs正确率散点图"""
        difficulties = [qa.difficulty for qa in question_analysis]
        accuracies = [qa.correct_rate * 100 for qa in question_analysis]
        question_ids = [qa.question_id for qa in question_analysis]
        question_types = [qa.question_type for qa in question_analysis]
        
        # 根据题目类型设置颜色
        type_colors = {
            '概念题': self.colors['primary'],
            '计算题': self.colors['success'],
            '证明题': self.colors['warning'],
            '编程题': self.colors['info']
        }
        
        fig = go.Figure()
        
        for qtype in set(question_types):
            type_indices = [i for i, qt in enumerate(question_types) if qt == qtype]
            
            fig.add_trace(go.Scatter(
                x=[difficulties[i] for i in type_indices],
                y=[accuracies[i] for i in type_indices],
                mode='markers',
                name=qtype,
                text=[question_ids[i] for i in type_indices],
                marker=dict(
                    size=12,
                    color=type_colors.get(qtype, self.colors['primary']),
                    opacity=0.7
                )
            ))
        
        fig.update_layout(
            title="题目难度 vs 正确率分析",
            xaxis_title="难度系数",
            yaxis_title="正确率 (%)",
            **self.default_layout
        )
        
        return fig
    
    def create_question_heatmap(self, question_analysis: List[QuestionAnalysis]) -> go.Figure:
        """创建题目分析热力图"""
        if not question_analysis:
            # Create an empty figure if no data
            fig = go.Figure()
            fig.update_layout(
                title="题目分析热力图",
                xaxis_title="题目编号",
                yaxis_title="分析维度",
                **self.default_layout
            )
            return fig
        
        # Prepare data for heatmap
        questions = [q.question_id for q in question_analysis]
        metrics = ["难度系数", "正确率", "平均分"]
        
        # Create data matrix
        z_data = []
        for metric in metrics:
            row = []
            for q in question_analysis:
                if metric == "难度系数":
                    row.append(q.difficulty)
                elif metric == "正确率":
                    row.append(q.correct_rate)
                elif metric == "平均分":
                    row.append(q.avg_score / q.max_score if q.max_score > 0 else 0)
            z_data.append(row)
        
        fig = go.Figure(data=go.Heatmap(
            z=z_data,
            x=questions,
            y=metrics,
            colorscale='RdYlGn',
            reversescale=True,  # Reverse scale for better visualization
            showscale=True,
            colorbar=dict(title="数值"),
            text=z_data,
            texttemplate="%{text:.2f}",
            textfont={"size": 10},
        ))
        
        fig.update_layout(
            title="题目分析热力图",
            xaxis_title="题目编号",
            yaxis_title="分析维度",
            **self.default_layout
        )
        
        return fig


# 全局图表组件实例
chart_components = ChartComponents()

# 便捷函数
def create_score_distribution_chart(student_scores: List[StudentScore]) -> go.Figure:
    """创建成绩分布图的便捷函数"""
    return chart_components.create_score_distribution_histogram(student_scores)

def create_grade_pie_chart(student_scores: List[StudentScore]) -> go.Figure:
    """创建成绩等级饼图的便捷函数"""
    return chart_components.create_grade_distribution_pie(student_scores)

def create_question_accuracy_chart(question_analysis: List[QuestionAnalysis]) -> go.Figure:
    """创建题目正确率图的便捷函数"""
    return chart_components.create_question_accuracy_bar(question_analysis)

def create_knowledge_heatmap_chart(question_analysis: List[QuestionAnalysis]) -> go.Figure:
    """创建知识点热力图的便捷函数"""
    return chart_components.create_knowledge_heatmap(question_analysis)

def create_student_radar_chart(student_score: StudentScore) -> go.Figure:
    """创建学生雷达图的便捷函数"""
    return chart_components.create_student_radar_chart(student_score)

def create_error_analysis_chart(question_analysis: List[QuestionAnalysis]) -> go.Figure:
    """创建错误分析图的便捷函数"""
    return chart_components.create_error_analysis_bar(question_analysis)

def create_trend_chart(student_scores: List[StudentScore]) -> go.Figure:
    """创建趋势图的便捷函数"""
    return chart_components.create_score_trend_line(student_scores)

def create_difficulty_scatter_chart(question_analysis: List[QuestionAnalysis]) -> go.Figure:
    """创建难度散点图的便捷函数"""
    return chart_components.create_difficulty_vs_accuracy_scatter(question_analysis)

def create_question_heatmap_chart(question_analysis: List[QuestionAnalysis]) -> go.Figure:
    """创建题目热力图的便捷函数"""
    return chart_components.create_question_heatmap(question_analysis)
