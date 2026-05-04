# SmarTAI 前端配色配置指南

本前端基于 [Reflex](https://reflex.dev/) + [Radix Colors](https://www.radix-ui.com/colors)。
所有色彩调整都集中在 **3 个文件**，不需要改散落各处的硬编码颜色：

| 调整对象 | 改这里 |
|---|---|
| 全站主色（按钮、链接、当前页 highlight 等） | `smartai_v2/theme.py` 的 `theme = rx.theme(...)` 的 `accent_color` |
| 语义色映射（成功 / 警告 / 错误 / 完成绿等） | `smartai_v2/theme.py` 的 `COLOR` 字典 |
| Stepper 五种状态颜色 | `smartai_v2/components/task_stepper.py` 的 `_step()` 函数 |

---

## 1. 全局主题色 (rx.theme)

**位置**：[`smartai_v2/theme.py`](theme.py) 第 24-30 行

```python
theme = rx.theme(
    appearance="light",        # light / dark
    accent_color="indigo",     # ← 主色 (按钮、链接、stepper 进行中)
    gray_color="slate",        # ← 灰阶基调
    radius="medium",           # small / medium / large / full
    scaling="100%",
)
```

`accent_color` 可选值（27 种 Radix 色板）：

```
gray, mauve, slate, sage, olive, sand,
tomato, red, ruby, crimson, pink, plum, purple, violet, iris, indigo, blue,
cyan, teal, jade, green, grass, brown, bronze, gold, yellow, amber, orange,
sky, mint, lime
```

> **示例改色**：把上面那一行改成 `accent_color="blue"`，全站主色就从靛蓝换成天蓝。

---

## 2. 语义色映射 (COLOR 字典)

**位置**：[`smartai_v2/theme.py`](theme.py) 第 35-50 行

```python
COLOR = {
    "primary":          "var(--indigo-9)",     # 主操作色 (确认按钮、active highlight)
    "primary_hover":    "var(--indigo-10)",
    "primary_subtle":   "var(--indigo-3)",     # 浅主色 (highlight 背景、按钮 hover)
    "accent":           "var(--green-9)",      # 完成绿 (stepper 完成、OK 徽章)
    "accent_subtle":    "var(--green-3)",
    "danger":           "var(--red-9)",        # 错误 / 删除按钮
    "warning":          "var(--amber-9)",      # 警告 / 低置信度 / flag
    "warning_subtle":   "var(--amber-3)",
    "bg":               "var(--slate-1)",      # 页面底色
    "surface":          "white",               # 卡片底色
    "text":             "var(--slate-12)",     # 主要文本
    "text_muted":       "var(--slate-10)",     # 次要文本（hint、help text）
    "border":           "var(--slate-5)",      # 卡片边框
    "component_bg":     "var(--slate-2)",      # 内嵌组件底（input 内 / chip 底）
    "bg_paper":         "var(--slate-1)",      # 学生答题等"纸张"质感底
}
```

**Radix 色阶规则**：`var(--{color}-{step})`，step 1-12

| Step | 用途 |
|---|---|
| 1-2 | 页面 / 卡片底色 |
| 3-5 | 浅色背景 / 边框 |
| 6-8 | 装饰性边框 |
| **9** | **品牌主色 / solid 按钮底** |
| 10 | 主色 hover |
| 11-12 | 文字色（在浅底上） |

**改色示例**：
```python
# 把"完成绿"换成蓝色：
"accent":  "var(--blue-9)",
"accent_subtle":  "var(--blue-3)",

# 把警告色从琥珀换成橙色：
"warning":  "var(--orange-9)",
```

> 改完 `COLOR` 字典后，**所有引用了对应键的组件都会自动跟随**——不需要再改组件。

---

## 3. Stepper（左侧引导栏）配色

**位置**：[`smartai_v2/components/task_stepper.py`](components/task_stepper.py) 的 `_step()` 函数

五种状态：

| 状态 | 触发条件 | 默认配色 |
|---|---|---|
| 未完成 + 未当前 | 还没走到 + 不在该路由 | 浅灰圈 + 灰图灰字 |
| 未完成 + 当前 | 还没走到 + 但点开了该页 | 蓝竖条 + 蓝填充 + 蓝图蓝粗字 |
| 已完成 + 未当前 | 流程已过 + 不在该路由 | 绿 outline + 绿对勾 |
| 已完成 + 当前 | 流程已过 + 仍在该页 | 蓝竖条 + 浅蓝底 + 绿对勾 |
| 进行中 (in_progress) | 当前 phase 正在执行 | 蓝竖条 + 蓝实心 + spinner |

颜色调整位置见 `_step()` 函数中的 `bg_color` / `fg_color` / `border_color` 三个变量。

---

## 4. 按钮颜色

`rx.button` **默认跟随** `theme.accent_color`，所以全站主操作按钮自动是 indigo 色。

如果某个按钮要 _显式_ 用别的颜色：

```python
# 删除按钮 — 红色
rx.button("删除", color_scheme="red", variant="solid")

# 取消 — 灰色 soft
rx.button("取消", color_scheme="gray", variant="soft")

# 强调成功操作 — 绿色
rx.button("批改完成", color_scheme="green", variant="solid")
```

`color_scheme` 取值同上面 `accent_color`。

`variant` 四种：
- `solid`（实心，默认主操作）
- `soft`（浅底，次要操作）
- `outline`（描边，备选）
- `ghost`（无背景，纯文字）

---

## 5. 徽章 (rx.badge)

```python
rx.badge("OK",   color_scheme="green",  variant="soft")
rx.badge("低分", color_scheme="amber",  variant="solid")
rx.badge("F",    color_scheme="red",    variant="solid")
```

---

## 6. Plotly 图表配色

**位置**：[`smartai_v2/theme.py`](theme.py) 的 `MACARON_COLORS` 列表

```python
MACARON_COLORS = [
    "var(--indigo-8)", "var(--blue-8)", "var(--cyan-8)", "var(--teal-8)",
    "var(--green-8)", "var(--amber-8)", "var(--orange-8)", "var(--crimson-8)",
    "var(--plum-8)",
]
```

NL 自然语言生成图表 + 内置图表都从这里循环取色。改这一个列表即可全站换图表配色。

---

## 7. 自定义 CSS 变量参考

完整的 Radix Colors 列表（每个色板都有 1-12 + alpha 版本）：
👉 https://www.radix-ui.com/colors

浏览器开发者工具能看到所有 `var(--xxx-N)`：检查任何元素 → Computed → 搜 `--`。

---

## 8. 常见调整速查

| 想做什么 | 改哪里 |
|---|---|
| 全站换主色（蓝→紫之类） | `theme.py` accent_color |
| "完成"图标颜色不对 | `theme.py` COLOR["accent"] |
| 警告 / 低置信度颜色 | `theme.py` COLOR["warning"] |
| Stepper 当前页高亮 | `components/task_stepper.py` `_step()` |
| 图表配色（饼图 / 柱图） | `theme.py` MACARON_COLORS |
| 单独某个按钮 | 在那行加 `color_scheme="..."` |
| 卡片边框 / 阴影 | `theme.py` `CARD_STYLE` 字典 |

---

## 9. 暗色模式

把 `theme.py` 第 24 行 `appearance="light"` 改成 `appearance="dark"` 即可。
所有 `var(--xxx-N)` 会自动切换到对应暗色色阶——**不需要改任何组件代码**。

> 注意：自定义的 `box_shadow` 可能在暗模式下对比不足，需要单独调。
