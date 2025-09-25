"""
SmarTAI Project Utilities Module

This module contains utility functions and classes for the SmarTAI intelligent assessment platform.
"""

__version__ = "1.0.0"
__author__ = "SmarTAI Team"

from .data_loader import *
from .chart_components import *

__all__ = [
    'StudentScore',
    'AssignmentStats', 
    'QuestionAnalysis',
    'DataLoader',
    'ChartComponents'
]