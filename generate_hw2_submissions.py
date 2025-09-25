import random
import os

# Student data
students = [
    ("赵六", "PB20141665"), ("钱七", "PB20151635"), ("孙八", "PB20161673"),
    ("周九", "PB20171682"), ("吴十", "PB20181674"), ("郑一", "PB20191644"),
    ("王二", "PB20201606"), ("冯三", "PB20211695"), ("陈四", "PB20221647"),
    ("褚五", "PB20231672"), ("卫六", "PB20241669"), ("蒋七", "PB20251615"),
    ("沈八", "PB20261681"), ("韩九", "PB20271665"), ("杨十", "PB20281688"),
    ("朱一", "PB20291692"), ("秦二", "PB20301680")
]

# Question options
q1_options = ["A", "B", "C", "D", "E"]
q2_options = ["A", "B", "C", "D"]
q6_options = ["A", "B", "C", "D"]

# Correct answers
correct_answers = {
    "q1": "E",
    "q2": "B",
    "q6": "C"
}

# Generate varied responses for each student
def generate_student_submission(name, sid):
    # For multiple choice questions, some students get correct answers, others get random ones
    q1_answer = correct_answers["q1"] if random.random() > 0.3 else random.choice(q1_options)
    q2_answer = correct_answers["q2"] if random.random() > 0.2 else random.choice(q2_options)
    q6_answer = correct_answers["q6"] if random.random() > 0.3 else random.choice(q6_options)
    
    # For calculation questions, generate varied but plausible responses
    # Q3: Recurrence relation (Calculation problem)
    if random.random() > 0.4:
        q3_answer = """递推关系 $a_n = 3a_{n-1} + 2a_{n-2}$ 的特征方程为 $x^2 - 3x - 2 = 0$

解得特征根为 $x_1 = \\frac{3 + \\sqrt{17}}{2}, x_2 = \\frac{3 - \\sqrt{17}}{2}$

通解为 $a_n = C_1 x_1^n + C_2 x_2^n$

代入初始条件 $a_0 = 1, a_1 = 2$ 解得系数。"""
    else:
        q3_answer = """特征方程为 $x^2 - 3x - 2 = 0$，解得 $x = \\frac{3 \\pm \\sqrt{17}}{2}$

因此通解为 $a_n = c_1(\\frac{3 + \\sqrt{17}}{2})^n + c_2(\\frac{3 - \\sqrt{17}}{2})^n$

利用初始条件确定常数。"""
    
    # Q4: Karnaugh map (Digital circuit problem)
    if random.random() > 0.3:
        q4_answer = """通过卡诺图化简函数 $F(A,B,C,D) = \\sum m(0,1,2,4,5,6,8,9,12,13,14)$

得到最简表达式为：$F = \\overline{A}\\overline{C} + \\overline{B}\\overline{D} + A\\overline{B}C$"""
    else:
        q4_answer = """使用卡诺图化简布尔函数，通过圈组得到：

$F = A'B' + B'D' + A'CD'$"""
    
    # Q5: Programming problem (Fibonacci with dynamic programming)
    if random.random() > 0.2:
        q5_answer = """```python
def fibonacci(n):
    if n <= 1:
        return n
    
    # 动态规划数组方法
    dp = [0] * (n + 1)
    dp[0] = 0
    dp[1] = 1
    
    for i in range(2, n + 1):
        dp[i] = dp[i-1] + dp[i-2]
    
    return dp[n]
```"""
    else:
        q5_answer = """```python
def fib_dp(n):
    if n <= 1:
        return n
    
    # 空间优化的动态规划方法
    prev2, prev1 = 0, 1
    
    for i in range(2, n + 1):
        current = prev1 + prev2
        prev2, prev1 = prev1, current
    
    return prev1
```"""
    
    # Q7: Graph theory proof (Proof problem)
    if random.random() > 0.2:
        q7_answer = """证明：在任何无向图中，度数为奇数的顶点个数必为偶数。

根据握手定理，所有顶点度数之和等于边数的两倍，为偶数。

将顶点按度数奇偶性分组，奇度数顶点的度数之和也必须为偶数。

因为奇数个奇数之和为奇数，偶数个奇数之和为偶数，所以奇度数顶点个数必为偶数。"""
    else:
        q7_answer = """设无向图中奇度数顶点有k个，它们的度数分别为 $d_1, d_2, ..., d_k$。

根据握手定理：$\\sum_{i=1}^{n} d_i = 2|E|$（偶数）

因为偶度数顶点度数之和也是偶数，所以 $\\sum_{i=1}^{k} d_i$ 必为偶数。

由于每个 $d_i$ 都是奇数，只有偶数个奇数相加才能得到偶数，因此k为偶数。"""
    
    content = f"""# {sid} {name} 第二次作业

## 1.

{q1_answer}

## 2.

{q2_answer}

## 3.

{q3_answer}

## 4.

{q4_answer}

## 5.

{q5_answer}

## 6.

{q6_answer}

## 7.

{q7_answer}
"""
    
    return content

# Create files for all students
output_dir = "test_docs_txt/SmarTAI_test_stu_hw2"
os.makedirs(output_dir, exist_ok=True)

for name, sid in students:
    filename = f"{sid}_{name}_作业2.txt"
    filepath = os.path.join(output_dir, filename)
    
    content = generate_student_submission(name, sid)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"Generated {filename}")

print(f"Successfully generated 17 student submissions in {output_dir}")