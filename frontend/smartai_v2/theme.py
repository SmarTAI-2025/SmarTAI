"""Design system tokens and shared style helpers.

颜色定义集中在这里 — 任何颜色调整都改这一个文件。详见 COLORS.md。

色彩映射理由：
  - accent_color="indigo": 选 indigo 而非 grass，是因为绿色基调显得像学校
    系统而非企业产品。indigo 中性且有专业感。
  - COLOR["primary"]: 蓝色，主操作按钮 + 当前页 highlight + 进行中状态
  - COLOR["accent"]:   绿色，仅用于"已完成 / 成功"语义
  - COLOR["danger"]:   红色，错误 / 删除
  - COLOR["warning"]:  amber，低置信度 / 标记
"""
from __future__ import annotations

import reflex as rx

# ─── 全局主题 ─────────────────────────────────────────────────────────────────
# accent_color 决定 rx.button / rx.link 等组件的默认色。改这一行就是全站换主色。
# 可选值: gray / mauve / slate / sage / olive / sand / tomato / red / ruby /
#         crimson / pink / plum / purple / violet / iris / indigo / blue / cyan /
#         teal / jade / green / grass / brown / bronze / gold / yellow / amber /
#         orange / sky / mint / lime
theme = rx.theme(
    appearance="light",
    accent_color="indigo",
    gray_color="slate",
    radius="medium",
    scaling="100%",
)

# ─── 语义色变量 ───────────────────────────────────────────────────────────────
# 用 var(--xxx-N) 引用 Radix Colors，N=1..12 是色阶（1 最浅、9 主色、12 最深）。
# 改色：把 "var(--indigo-9)" 替换成 "var(--blue-9)" 即可。
COLOR = {
    "primary":          "var(--indigo-9)",     # 主操作 (按钮、链接、stepper 当前/进行中)
    "primary_hover":    "var(--indigo-10)",
    "primary_subtle":   "var(--indigo-3)",     # 浅主色 (highlight 背景、按钮 hover)
    "accent":           "var(--green-9)",      # 完成绿 (stepper 完成圈、OK 徽章)
    "accent_subtle":    "var(--green-3)",
    "danger":           "var(--red-9)",        # 错误 / 删除
    "warning":          "var(--amber-9)",      # 警告 / 低置信
    "warning_subtle":   "var(--amber-3)",
    "bg":               "var(--slate-1)",      # 页面底色
    "surface":          "white",               # 卡片底色
    "text":             "var(--slate-12)",     # 主要文本
    "text_muted":       "var(--slate-10)",     # 次要文本
    "border":           "var(--slate-5)",      # 卡片边框
    "component_bg":     "var(--slate-2)",      # 内嵌组件底
    "bg_paper":         "var(--slate-1)",      # 学生答题等"纸张"质感底
}

# ─── 强调色调色板 (用于 chart trace 颜色) ─────────────────────────────────────
MACARON_COLORS = [
    "var(--indigo-8)",
    "var(--blue-8)",
    "var(--cyan-8)",
    "var(--teal-8)",
    "var(--green-8)",
    "var(--amber-8)",
    "var(--orange-8)",
    "var(--crimson-8)",
    "var(--plum-8)",
]

SPACE = {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "48px"}
RADIUS = {"sm": "6px", "md": "12px", "lg": "16px", "full": "9999px"}
SHADOW = {
    "sm": "0 1px 2px rgba(0,0,0,0.04)",
    "md": "0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)",
    "lg": "0 4px 12px rgba(0,0,0,0.08)",
}
FONT = {
    "sans": "Inter, 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif",
    "mono": "'JetBrains Mono', monospace",
}

CARD_STYLE = {
    "background": COLOR["surface"],
    "border": f"1px solid {COLOR['border']}",
    "border_radius": RADIUS["md"],
    "box_shadow": SHADOW["sm"],
    "padding": SPACE["lg"],
}

PAGE_STYLE = {
    "background": COLOR["bg"],
    "min_height": "100vh",
    "font_family": FONT["sans"],
    "color": COLOR["text"],
}


STATUS_COLORS = {
    "completed": COLOR["accent"],
    "pending": COLOR["warning"],
    "error": COLOR["danger"],
    "not_found": COLOR["text_muted"],
}
