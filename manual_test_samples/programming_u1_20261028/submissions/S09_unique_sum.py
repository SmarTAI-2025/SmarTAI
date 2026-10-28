def sum_positive(nums):
    return sum(set(x for x in nums if x > 0))
