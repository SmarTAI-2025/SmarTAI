# 构造作答材料：题号倒序

本文件与S07为同一构造学生的另一种输入形式，不能作为第二次提交重复计分。普通文本映射流程本轮未执行。

## 第3题（PY-F03）

对应入口 count_positive(nums)。

## 第2题（PY-F02）

对应入口 sum_positive(nums)。

## 两题共用完整源码

```python
def count_positive(nums):
    return sum(1 for x in nums if is_positive(x))

def is_positive(x):
    return x > 0

def sum_positive(nums):
    return sum(x for x in nums if is_positive(x))
```
