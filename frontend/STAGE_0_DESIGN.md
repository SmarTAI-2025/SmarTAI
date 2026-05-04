# Stage 0 详细设计 — Reflex 前端重构

> **范围**：从零搭建 `frontend_v2/`，覆盖现有 Streamlit 14 页全部功能（教师端为主），并预留学生端骨架。
> **不包含**：Streamlit 修补（直接跳过原计划阶段 0，进入 Reflex 实现）；后端 PostgreSQL/JWT 实现（写一份接口契约文档让后端实现）。

---

## 1. 项目布局

```
frontend_v2/
├── rxconfig.py                  # Reflex 配置（app_name, backend_uri, frontend_port）
├── requirements.txt             # reflex, httpx, plotly, pydantic
├── README.md                    # 启动指南
├── STAGE_0_DESIGN.md            # 本文档
├── BACKEND_STUB_ENDPOINTS.md    # 后端待实现接口契约
│
└── smartai_v2/                  # Reflex 包目录（与 rxconfig.app_name 同名）
    ├── __init__.py
    ├── smartai_v2.py            # 入口：rx.App() + 路由注册
    ├── config.py                # 后端 URL、常量
    ├── theme.py                 # 设计系统（colors, fonts, sizes, radius）
    │
    ├── api/                     # 后端 HTTP 客户端（httpx async）
    │   ├── __init__.py
    │   ├── client.py            # 共享 httpx.AsyncClient + 错误处理
    │   ├── auth.py              # /auth/login, /register, /me（待后端实现）
    │   ├── ingest.py            # /prob_preview/, /hw_preview/
    │   ├── grading.py           # /ai_grading/* + SSE consumer
    │   ├── experts.py           # /experts/* BYOK
    │   └── human_edit.py        # /human_edit/*
    │
    ├── state/                   # Reflex State 类
    │   ├── __init__.py
    │   ├── base.py              # BaseState（共享辅助）
    │   ├── auth.py              # AuthState（token, user, role）
    │   ├── ingest.py            # IngestState（prob_data, stu_data, upload progress）
    │   ├── grading.py           # GradingState（jobs, current_job, progress, results）
    │   ├── experts.py           # ExpertsState（providers, BYOK 配置）
    │   └── ui.py                # UIState（modals, toasts, sidebar collapsed）
    │
    ├── components/              # 可复用组件
    │   ├── __init__.py
    │   ├── layout.py            # 应用外壳：Sidebar + Topbar + 主内容区
    │   ├── auth_guard.py        # require_auth() 装饰器
    │   ├── cards.py             # stat_card, feature_card, info_card
    │   ├── progress.py          # SSE 进度条 + 阶段标记
    │   ├── tables.py            # data_table 简单封装
    │   ├── charts.py            # Plotly 包装（评分分布、雷达图）
    │   └── forms.py             # 表单输入 + 校验提示
    │
    └── pages/                   # 路由页面
        ├── __init__.py
        ├── login.py             # /login
        ├── register.py          # /register
        ├── dashboard.py         # /  教师 Dashboard
        ├── student_dashboard.py # /student
        ├── prob_upload.py       # /upload/problems
        ├── hw_upload.py         # /upload/homework
        ├── problems.py          # /problems
        ├── students.py          # /students
        ├── student_detail.py    # /students/[id]
        ├── grading.py           # /grading/[job_id]   ← SSE 实时进度
        ├── results.py           # /results/[job_id]
        ├── score_report.py      # /reports
        ├── visualization.py     # /visualization
        ├── history.py           # /history
        ├── experts.py           # /experts            ← 新增 BYOK 配置 UI
        ├── knowledge_base.py    # /knowledge-base
        └── settings.py          # /settings
```

---

## 2. 设计系统（theme.py）

| Token | 值 |
|---|---|
| `primary` | `indigo.9` (`#4F46E5`) |
| `accent` | `emerald.9` (`#10B981`) |
| `danger` | `red.9` (`#DC2626`) |
| `bg` | `slate.1` (`#F8FAFC`) |
| `surface` | `white` |
| `text` | `slate.12` (`#0F172A`) |
| `muted` | `slate.10` (`#64748B`) |
| `border` | `slate.5` (`#E2E8F0`) |
| `radius_sm/md/lg` | `6px / 12px / 16px` |
| `space` | `4 / 8 / 16 / 24 / 48 px` |
| `font` | Inter (en) + Noto Sans SC (zh) |

Reflex 的 `rx.theme` 接受 Radix `accent_color`、`gray_color`、`radius`、`scaling` —— 我们用 `accent_color="indigo"`、`gray_color="slate"`、`radius="medium"`。

---

## 3. 路由表（替换 Streamlit 14 页）

| 旧 Streamlit 路径 | 新 Reflex 路由 | 角色 | 说明 |
|---|---|---|---|
| `pages/login.py` | `/login` | 公开 | 真登录（JWT） |
| - | `/register` | 公开 | 教师邀请码注册 |
| `pages/main.py` | `/` | teacher | 教师 Dashboard |
| - | `/student` | student | 学生 Dashboard（新增） |
| `pages/prob_upload.py` | `/upload/problems` | teacher | 题目上传 |
| `pages/hw_upload.py` | `/upload/homework` | teacher | 学生作业批量上传 |
| `pages/problems.py` | `/problems` | teacher | 题目列表 + 编辑 |
| `pages/stu_preview.py` | `/students` | teacher | 学生答案预览 |
| `pages/stu_details.py` | `/students/[id]` | teacher | 单个学生详情 |
| `pages/wait_ai_grade.py` | `/grading/[job_id]` | teacher | **SSE 实时进度** |
| `pages/grade_results.py` | `/results/[job_id]` | teacher | 批改结果详情 |
| `pages/score_report.py` | `/reports` | teacher | 班级成绩报告 |
| `pages/visualization.py` | `/visualization` | teacher | 可视化图表 |
| `pages/history.py` | `/history` | teacher | 历史任务 |
| - | `/experts` | teacher | **BYOK 多专家配置（新增 UI）** |
| `pages/knowledge_base.py` | `/knowledge-base` | teacher | 知识库（占位，等 P2 RAG） |
| `pages/backend_status.py` | `/settings` | teacher | 系统设置（含连接诊断） |

---

## 4. 状态管理设计

### 跨页持久（Reflex `LocalStorage`）
- `AuthState.token` — JWT，刷新页面不丢
- `AuthState.user` — 登录用户基本资料
- `UIState.sidebar_collapsed` — 侧栏折叠状态

### 跨页内存（继承 `rx.State`）
- `IngestState`：`prob_data`（dict[q_id → 题目]）、`stu_data`（dict[stu_id → 学生答案]）
- `GradingState`：`jobs`（活跃 job 列表）、`current_job_id`、`progress`（SSE 推送）
- `ExpertsState`：`providers`（已配置的 LLM provider 列表）

### 私有（继承 `rx.LocalState` 或页面级）
- 表单临时值、模态开关等

### SSE 进度策略
后端 `GET /ai_grading/progress/{id}/stream` 返回 SSE 流。Reflex 没有原生 EventSource，我们用 **后台轮询任务**（更可靠、更简单）：

```python
class GradingState(rx.State):
    progress: dict = {}
    polling: bool = False

    @rx.event(background=True)
    async def watch_progress(self, job_id: str):
        async with self:
            self.polling = True
        while self.polling:
            snapshot = await api.grading.get_progress(job_id)
            async with self:
                self.progress = snapshot
                if snapshot.get("phase") == "done":
                    self.polling = False
                    break
            await asyncio.sleep(1.5)
```

刷新间隔 1.5s（比 Streamlit 5s 强 3 倍，且不重载页面）。后续可改 `httpx.stream()` 升级到真 SSE。

---

## 5. SSE 数据契约（后端已实现）

`GET /ai_grading/progress/{job_id}/stream` 推送的 JobProgress（[backend/models.py](backend/models.py) 中定义）：

```json
{
  "phase": "ingest|grading|synthesis|done|error",
  "completed_students": 12,
  "total_students": 50,
  "completed_questions": 240,
  "total_questions": 500,
  "current_step": "Grading student S023, question Q3",
  "events": [
    {"timestamp": "...", "level": "info", "message": "..."}
  ]
}
```

UI 显示：
- 顶部：阶段卡片（4 个圆点：ingest → grading → synthesis → done）
- 中部：双进度条（学生维度 + 题目维度）
- 底部：事件日志（auto-scroll）

---

## 6. 与现有 Streamlit 共存策略

- `frontend/`（Streamlit）保持不动，可继续 `streamlit run` 验证。
- `frontend_v2/`（Reflex）端口 3000（前端）+ 8001（Reflex 后端）。
- 后端 FastAPI（端口 8000）的 [CORS 白名单](backend/main.py#L73-L80) 加 `http://localhost:3000`、`http://localhost:8001`。
- 验证通过后下线 `frontend/`。

---

## 7. 启动流程

```bash
cd frontend_v2
pip install -r requirements.txt
reflex init --template blank      # 仅首次（已通过 rxconfig.py 跳过）
reflex run                        # 默认 :3000 前端 + :8000 Reflex 后端
```

后端单独跑：
```bash
cd backend
uvicorn backend.main:app --reload --port 8000
```

---

## 8. 后端依赖（待后端实现）

`frontend_v2/BACKEND_STUB_ENDPOINTS.md` 详细列出需要后端补的端点（auth、users、courses、assignments、submissions、grades）。前端代码会用 try/except 优雅降级——后端没就绪时，登录页可用"开发模式 demo 登录"（前端模拟 token）跳过认证看 UI。
