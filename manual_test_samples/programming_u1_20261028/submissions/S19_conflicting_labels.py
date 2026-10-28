# 第3题 (PY-F03)
def sum_positive(nums):
    return sum(x for x in nums if x > 0)

# 第2题 (PY-F02)
def count_positive(nums):
    return sum(1 for x in nums if x > 0)
