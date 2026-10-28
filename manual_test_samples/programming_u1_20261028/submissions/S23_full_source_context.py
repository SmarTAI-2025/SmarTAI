import operator

POSITIVE_BOUNDARY = 0

# 第3题（PY-F03）：定义顺序在第2题之前
def count_positive(nums):
    return sum(1 for value in nums if is_positive(value))

# 第2题（PY-F02）
def sum_positive(nums):
    return sum(value for value in nums if is_positive(value))

# 两题共用，置于最后；依赖顶部import和常量
def is_positive(value):
    return operator.gt(value, POSITIVE_BOUNDARY)
