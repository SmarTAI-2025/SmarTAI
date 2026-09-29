"""Small, auditable terminology bridges, not a general semantic translator.

Aliases enrich the local index only. They never rewrite source text, citations,
student answers or model prompts, and do not require a provider call.
"""
import re


TERM_ALIASES = {
    "homomorphism": ("homomorphism", "homomorphisms", "同态"),
    "isomorphism": ("isomorphism", "isomorphisms", "同构"),
    "injective": ("injective", "injection", "one to one", "单射", "一一映射"),
    "surjective": ("surjective", "surjection", "onto", "满射"),
    "bijective": ("bijective", "bijection", "双射"),
    "kernel": ("kernel", "kernels", "核空间", "零空间", "null space", "nullspace"),
    "eigenvalue": ("eigenvalue", "eigenvalues", "特征值"),
    "eigenvector": ("eigenvector", "eigenvectors", "特征向量"),
    "determinant": ("determinant", "determinants", "行列式"),
    "rank": ("matrix rank", "矩阵的秩", "矩阵秩"),
    "linear_independence": ("linear independence", "linearly independent", "线性无关"),
    "linear_dependence": ("linear dependence", "linearly dependent", "线性相关"),
    "derivative": ("derivative", "derivatives", "导数", "微商", "瞬时变化率"),
    "integral": ("integral", "integrals", "积分"),
    "continuity": ("continuity", "continuous", "连续性", "连续函数"),
    "convergence": ("convergence", "convergent", "converges", "收敛"),
    "conditional_probability": ("conditional probability", "条件概率"),
    "independence": ("independent events", "独立事件", "事件独立"),
    "bayes": ("bayes", "bayesian", "贝叶斯", "贝氏"),
    "expectation": ("expected value", "expectation", "数学期望", "期望值"),
    "variance": ("variance", "方差"),
    "momentum": ("momentum", "动量"),
    "kinetic_energy": ("kinetic energy", "动能"),
    "entropy": ("entropy", "熵"),
    "binary_search": ("binary search", "二分查找", "二分搜索", "折半查找"),
    "recursion": ("recursion", "recursive", "递归"),
    "time_complexity": ("time complexity", "时间复杂度"),
}


def _pattern(alias):
    # ASCII word boundaries prevent 'onto' matching 'ontology', for example.
    escaped = re.escape(alias).replace(r"\ ", r"[\s-]+")
    return r"(?<![a-z])" + escaped + r"(?![a-z])" if alias.isascii() else escaped


_PATTERNS = [("term:" + key, re.compile("|".join(_pattern(alias) for alias in aliases)))
             for key, aliases in TERM_ALIASES.items()]


def concept_terms(normalized):
    return {key for key, pattern in _PATTERNS if pattern.search(normalized)}


QUERY_BOILERPLATE = re.compile(
    r"请问|请解释|解释一下|如何|为什么|是什么|怎么|教材|课本|相关内容|相关知识|"
    r"练习|习题|例题|题号|章节|证明|求出|计算|说明|定义|定理|的|了|吗|呢"
)
