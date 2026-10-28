def first(nums):
    return sum(x for x in nums if x > 0)

def second(nums):
    return sum(1 for x in nums if x > 0)
