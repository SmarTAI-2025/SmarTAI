"""
PDF Report Generator for SmarTAI

This module provides functionality to generate PDF reports from grading data.
"""

import streamlit as st
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any
import os
import tempfile

# Register Chinese font if available
try:
    # Try to register a Chinese font
    # You may need to download a Chinese font file and place it in the project directory
    # For example, you can download simhei.ttf (Simplified Chinese Hei font)
    pdfmetrics.registerFont(TTFont('SimHei', 'simhei.ttf'))
    CHINESE_FONT = 'SimHei'
except:
    # Fallback to default font
    CHINESE_FONT = 'Helvetica'

def generate_assignment_report(assignment_stats: Any, students: List[Any], 
                             question_analysis: List[Any], filename: str = None) -> str:
    """
    Generate a PDF report for an assignment.
    
    Args:
        assignment_stats: Assignment statistics data
        students: List of student scores
        question_analysis: List of question analysis data
        filename: Output filename (optional)
        
    Returns:
        Path to the generated PDF file
    """
    # Create a temporary file
    if filename is None:
        filename = f"assignment_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, filename)
    
    # Create PDF
    c = canvas.Canvas(file_path, pagesize=A4)
    width, height = A4
    
    # Title
    c.setFont(CHINESE_FONT, 18)
    c.drawCentredString(width/2, height-50, assignment_stats.assignment_name)
    c.setFont(CHINESE_FONT, 12)
    c.drawCentredString(width/2, height-70, f'报告生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    
    # Assignment Statistics
    c.setFont(CHINESE_FONT, 14)
    c.drawString(50, height-100, '作业统计信息')
    c.setFont(CHINESE_FONT, 12)
    
    stats_data = [
        ['统计项目', '数值'],
        ['学生总数', str(assignment_stats.total_students)],
        ['提交人数', str(assignment_stats.submitted_count)],
        ['平均分', f'{assignment_stats.avg_score:.2f}'],
        ['最高分', f'{assignment_stats.max_score:.2f}'],
        ['最低分', f'{assignment_stats.min_score:.2f}'],
        ['标准差', f'{assignment_stats.std_score:.2f}'],
        ['及格率', f'{assignment_stats.pass_rate:.2f}%'],
        ['题目数量', str(assignment_stats.question_count)]
    ]
    
    # Create table for statistics
    table = Table(stats_data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), CHINESE_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    table.wrapOn(c, width, height)
    table.drawOn(c, 50, height-250)
    
    # Top Students
    c.setFont(CHINESE_FONT, 14)
    c.drawString(50, height-280, '学生成绩排行 (前10名)')
    c.setFont(CHINESE_FONT, 12)
    
    # Sort students by percentage
    sorted_students = sorted(students, key=lambda x: x.percentage, reverse=True)
    
    student_data = [['排名', '学号', '姓名', '总分', '百分比', '等级']]
    for i, student in enumerate(sorted_students[:10]):
        student_data.append([
            str(i + 1),
            student.student_id,
            student.student_name,
            f'{student.total_score:.1f}/{student.max_score}',
            f'{student.percentage:.2f}%',
            student.grade_level
        ])
    
    # Create table for top students
    student_table = Table(student_data)
    student_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), CHINESE_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    student_table.wrapOn(c, width, height)
    student_table.drawOn(c, 50, height-450)
    
    # Question Analysis
    if question_analysis:
        c.setFont(CHINESE_FONT, 14)
        c.drawString(50, height-480, '题目分析概览')
        c.setFont(CHINESE_FONT, 12)
        
        question_data = [['题目ID', '题型', '难度系数', '正确率', '平均分']]
        for question in question_analysis[:10]:  # Show first 10 questions
            question_data.append([
                question.question_id,
                question.question_type,
                f'{question.difficulty:.2f}',
                f'{question.correct_rate:.2%}',
                f'{question.avg_score:.2f}/{question.max_score}'
            ])
        
        # Create table for question analysis
        question_table = Table(question_data)
        question_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), CHINESE_FONT),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        question_table.wrapOn(c, width, height)
        question_table.drawOn(c, 50, height-650)
    
    # Save PDF
    c.save()
    
    return file_path

def generate_student_report(student: Any, filename: str = None) -> str:
    """
    Generate a PDF report for a specific student.
    
    Args:
        student: Student score data
        filename: Output filename (optional)
        
    Returns:
        Path to the generated PDF file
    """
    # Create a temporary file
    if filename is None:
        filename = f"student_report_{student.student_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, filename)
    
    # Create PDF
    c = canvas.Canvas(file_path, pagesize=A4)
    width, height = A4
    
    # Title
    c.setFont(CHINESE_FONT, 18)
    c.drawCentredString(width/2, height-50, f'{student.student_name} 的作业报告')
    c.setFont(CHINESE_FONT, 12)
    
    # Student information
    c.drawString(50, height-80, f'学号: {student.student_id}')
    c.drawString(50, height-100, f'提交时间: {student.submit_time.strftime("%Y-%m-%d %H:%M")}')
    
    # Score summary
    c.setFont(CHINESE_FONT, 14)
    c.drawString(50, height-130, '成绩概览')
    c.setFont(CHINESE_FONT, 12)
    
    score_data = [
        ['项目', '数值'],
        ['总分', f'{student.total_score:.1f}/{student.max_score}'],
        ['百分比', f'{student.percentage:.2f}%'],
        ['成绩等级', student.grade_level],
        ['置信度', f'{student.confidence_score:.2%}']
    ]
    
    # Create table for score summary
    table = Table(score_data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), CHINESE_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    table.wrapOn(c, width, height)
    table.drawOn(c, 50, height-200)
    
    # Question details
    if student.questions:
        c.setFont(CHINESE_FONT, 14)
        c.drawString(50, height-230, '题目详情')
        c.setFont(CHINESE_FONT, 12)
        
        y_position = height-260
        for i, question in enumerate(student.questions):
            c.setFont(CHINESE_FONT, 12)
            c.drawString(50, y_position, f'题目 {i+1}: {question["question_id"]}')
            y_position -= 20
            
            question_data = [
                ['项目', '内容'],
                ['题型', question['question_type']],
                ['得分', f'{question["score"]:.1f}/{question["max_score"]}'],
                ['置信度', f'{question["confidence"]:.2%}'],
                ['反馈', question.get('feedback', '无')]
            ]
            
            # Create table for question details
            question_table = Table(question_data)
            question_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), CHINESE_FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            
            question_table.wrapOn(c, width, height)
            question_table.drawOn(c, 50, y_position-80)
            y_position -= 120
    
    # Save PDF
    c.save()
    
    return file_path