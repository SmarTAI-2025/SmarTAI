def is_positive(x):
    return x > 0

def sum_positive(nums):
    return sum(x for x in nums if is_positive(x))

def count_positive(nums):
    return sum(1 for x in nums if is_positive(x))
