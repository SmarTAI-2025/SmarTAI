# SmarTAI Vite React 前端重构状态总表

更新时间：2026-07-01 约19:55 CST
分支：`codex/vite-react-frontend`  
范围：React/Vite 教师端主流程。旧 Reflex 前端保留为回退路径。

## 1. 产品边界

当前展示范围：

- 登录页 `/login` 与注册申请页 `/register`。
- 登录后教师端批改工作台：Dashboard、History、New Task。
- 批改配置 Setup。
- 上传题目、题目预览与题干/评分标准编辑。
- 上传学生作答、学生作答预览与识别结果编辑。
- 启动批改、进度轮询、完成后进入结果。
- BYOK Experts 与 Settings。

当前可以展示但必须限制语义：

- 知识库只展示为“本任务参考资料”，使用后端已有 task-scoped KB。
- Setup 中可以上传、查看、删除本任务 KB 文档，并说明其只用于当前任务。
- 不声称已实现跨任务复用的个人全局知识库。

当前禁止展示：

- 学生端工作台。
- 课程管理、作业发布、学生提交、学生成绩页。
- LMS、LTI、SSO、成绩回传入口。
- Canvas、Moodle 或学校 LMS 连接配置入口。
- 会让用户以为 LMS 或用户级全局知识库已完成的页面、按钮、导航或空状态文案。

## 2. 批改语言与 UI 语言

- 全站 UI 已有中英文切换与深浅主题切换基础能力。
- 前端不展示批改语言选择。
- 当前 `GradeRequest.language` 仍由 React API client 集中封装兼容；后端自动识别批改语言留作后端 TODO。

## 3. 功能状态矩阵

| 功能 | 代码状态 | 用户可见 | 依赖后端新改动 | 备注 |
|---|---|---:|---:|---|
| Vite React App 工程 | 已完成 | 是 | 否 | `frontend/app` 可 typecheck/build |
| 登录 | 已接入真实登录 | 是 | 否 | 本地 React dev CORS 已同步 |
| 注册申请页 | 静态完成 | 是 | 否 | 保持“受邀/暂未开放”语义 |
| 教师 Dashboard | 已接入 `/tasks/` | 是 | 否 | 展示统计、近期任务、按状态跳转 |
| New Task | 已接入 `/tasks/` 创建 | 是 | 否 | 创建成功跳 `/tasks/{id}/setup` |
| History | 已接入 `/tasks/` 列表 | 是 | 否 | 支持刷新、删除、状态跳转 |
| Setup 批改配置 | 已接入 task、KB、experts | 是 | 否 | 不展示批改语言选择 |
| task-scoped KB 上传/删除 | 已接入 `/tasks/{id}/kb` | 是 | 否 | 文案限定为本任务资料 |
| 结果区 KB 状态反馈 | 已接入任务资料状态提示 | 是 | 否 | 显示配置/资料状态，不声称逐题引用命中 |
| 用户级全局知识库 | 未实现 | 否 | 是 | 见 `BACKEND_TODOS_CN.md` |
| 上传题目与题目编辑 | 已接入 | 是 | 否 | 支持上传、预览、题干/评分标准编辑 |
| 上传作答与作答编辑 | 已接入 | 是 | 否 | 支持上传、学生答案预览与编辑 |
| 批改启动与进度 | 已接入 | 是 | 否 | 使用 `/tasks/{id}/grade` 与 state polling |
| 结果总览 | 已接入真实结果 | 是 | 否 | 展示均分、低置信、复核队列、学生列表 |
| 按题分析 | 已接入真实结果 | 是 | 否 | 基于本地结果计算题目统计；可选读取易错点 API |
| 可视化 | 已接入基础图表与 NL chart | 是 | 否 | NL chart 使用白名单 trace 的轻量预览，避免重型 Plotly 构建风险 |
| 学生详情 | 已接入真实结果 | 是 | 否 | 支持上一位/下一位、返回总览、跳题目详情 |
| 题目详情 | 已接入真实结果 | 是 | 否 | 支持上一题/下一题、返回按题列表、跳学生详情 |
| BYOK Experts | 已接入真实 API | 是 | 否 | key 不回显；无共享池管理入口 |
| Settings | 基础可用 | 是 | 否 | 主题、语言、后端健康检查已接入 |
| 学生端 | 禁止展示 | 否 | 部分是 | 仅保留“暂未开放”提示页 |
| LMS/LTI 类型 | 隐藏预留 | 否 | 是 | 仅 `src/types/lms.ts`，未注册路由 |

## 4. 执行清单

时间为本地时间（Asia/Shanghai），取阶段完成或提交前确认的大致时间。

### 分支与脚手架

- [x] 创建分支 `codex/vite-react-frontend`（完成：2026-06-30 约15:40）
- [x] 创建 `frontend/app` Vite React + TypeScript 工程（完成：2026-06-30 约21:20）
- [x] 配置 Tailwind、lucide、sonner、TanStack Query（完成：2026-06-30 约21:20；Plotly 依赖已在 2026-07-01 约15:15 移除，改为轻量图表预览）
- [x] 配置 typecheck/build 脚本（完成：2026-06-30 约21:20）
- [x] 配置 `.env.example` 和 React README（完成：2026-06-30 约21:20）

### API Client

- [x] 统一 API client、auth header、错误处理（完成：2026-06-30 约21:20）
- [x] auth token 与 user LocalStorage（完成：2026-06-30 约21:20）
- [x] tasks、analytics、kb、experts、health hooks（完成：2026-06-30 约21:20）
- [x] 上传进度与 task state polling（完成：2026-06-30 约21:20）
- [x] 后端 CORS 默认白名单加入 React dev 地址（完成：2026-07-01 约14:10）

### Auth

- [x] 登录页真实接入（完成：2026-06-30 约21:50）
- [x] 注册申请页保持受邀/暂未开放语义（完成：2026-06-30 约21:20）
- [x] 教师端路由守卫（完成：2026-06-30 约21:50）
- [x] student 角色显示“学生端暂未开放”（完成：2026-06-30 约21:20）

### 教师主流程页面

- [x] Dashboard 接入任务统计与近期任务（完成：2026-07-01 约14:30，Worker A）
- [x] History 接入任务列表与删除（完成：2026-07-01 约14:30，Worker A）
- [x] New Task 接入创建任务并跳 Setup（完成：2026-07-01 约14:30，Worker A）
- [x] Setup 接入 task、专家概览与本任务 KB（完成：2026-07-01 约14:30，Worker D）
- [x] 上传题目、题目预览与编辑（完成：2026-07-01 约14:30，Worker E）
- [x] 上传作答、学生答案预览与编辑（完成：2026-07-01 约14:30，Worker E）
- [x] 启动批改与进度轮询 UI（完成：2026-07-01 约14:30，Worker E）
- [x] 结果总览接入真实批改结果、低置信与复核队列（完成：2026-07-01 约14:45，Worker F）
- [x] 按题分析接入本地结果统计，并可选读取 `/analytics/{id}/per_question/{q_id}` 易错点（完成：2026-07-01 约14:45，Worker F）
- [x] 可视化接入基础分布与按题均分图（完成：2026-07-01 约14:45，Worker F）
- [x] 学生详情接入真实数据、上一位/下一位、跳题目分析（完成：2026-07-01 约14:45，Worker F）
- [x] 题目详情接入真实数据、上一题/下一题、跳学生详情（完成：2026-07-01 约14:45，Worker F）
- [x] NL summary/filter/chart 交互接入（完成：2026-07-01 约15:15，Worker J）

### 知识库展示与实际后端能力

- [x] Setup 中只展示 task-scoped KB（完成：2026-07-01 约14:30，Worker D）
- [x] 本任务资料上传、列表、删除接入真实 hooks（完成：2026-07-01 约14:30，Worker D）
- [x] 文案避免“全局知识库/个人知识库已完成”的暗示（阶段检查：2026-07-01 约14:30）
- [x] 结果区显示 task-scoped KB 配置/资料状态反馈（完成：2026-07-01 约19:25，Agent UI / Pasteur；说明不声称逐题引用命中）

### BYOK 与 Settings

- [x] BYOK Experts 列表接入真实 API（完成：2026-07-01 约14:30，Worker G）
- [x] 添加 API key 表单，提交后清空 key（完成：2026-07-01 约14:30，Worker G）
- [x] 启停与删除 expert（完成：2026-07-01 约14:30，Worker G）
- [x] Settings 后端健康检查（完成：2026-06-30 约21:35）
- [x] 主题与 UI 语言切换（完成：2026-06-30 约21:20）

### 隐藏模块审计

- [x] 阶段性源码审计：导航无 LMS/课程/作业/学生端工作台入口（完成：2026-07-01 约14:30）
- [x] 阶段性源码审计：Setup 无批改语言选择（完成：2026-07-01 约14:30）
- [x] 阶段性源码审计：task KB 文案未声称全局复用（完成：2026-07-01 约14:30）
- [x] 静态范围审计脚本 `npm run audit:scope` 已接入（完成：2026-07-01 约15:10，Worker I）
- [x] 最终隐藏项审计：Router、Sidebar、Topbar、空状态、移动端截图（完成：2026-07-01 约19:45，Agent QA / Linnaeus + 主线程浏览器验收）

### 测试与构建

- [x] `npm run typecheck`（完成：2026-07-01 约19:05）
- [x] `npm run audit:scope`（完成：2026-07-01 约19:50）
- [x] `npm run lint`（完成：2026-07-01 约19:50；无新增依赖，当前执行 `audit:scope` + `typecheck`）
- [x] `npm run build`（完成：2026-07-01 约19:50；route-level code splitting 后无单包 chunk size warning）
- [x] Vite dev server HTTP smoke test（完成：2026-07-01 约19:47，`127.0.0.1:5173` 返回 200）
- [x] Browser/Playwright API 教师主流程验收（完成：2026-07-01 约19:45；本地未安装 `@playwright/test`，使用 Codex Browser 内置 Playwright API 验证）
- [x] 桌面与移动端截图检查（完成：2026-07-01 约19:45；截图保存于 `/private/tmp/smartai-react-smoke/`）
- [x] 阶段文档状态与实际代码一致性检查（完成：2026-07-01 约19:55）

## 5. 本轮 Agent 分工记录

| Agent | 范围 | 状态 | 验证 |
|---|---|---|---|
| Worker A / Dalton | Dashboard、History、New Task | 完成 | 子任务 typecheck 通过 |
| Worker D / Godel | Setup、task KB、experts 概览 | 完成 | 子任务 typecheck 通过 |
| Worker E / Carver | Upload、review、start grading | 完成 | 子任务 typecheck 通过 |
| Worker G / Mendel | BYOK Experts | 完成 | 子任务 typecheck 通过 |
| Worker F / Wegener | Results、student detail、question detail | 完成 | 子任务 typecheck 通过 |
| Worker J / Bacon | NL summary/filter/chart | 完成 | 子任务 typecheck 通过 |
| Worker I / Ptolemy | 静态范围审计脚本 | 完成 | audit:scope 通过 |
| Agent QA / Linnaeus | 最终可见范围审计、结果导航证据、浏览器验收清单 | 完成 | audit:scope 通过；报告 Router/Sidebar/Topbar 无隐藏范围泄漏 |
| Agent UI / Pasteur | 结果区 task-scoped KB 状态反馈 | 完成 | 子任务 typecheck 通过 |
| Agent Build / Zeno | route-level code splitting、无依赖 lint 脚本 | 完成 | typecheck/lint/build 通过；无 chunk warning |
| 主线程 | 集成验证、真实浏览器验收、截图、文档 | 完成 | lint/build 通过；登录、Setup、Results、Settings 浏览器验收通过 |

## 6. 下一阶段建议

下一阶段优先做正式 e2e 与后端能力补齐：

- 若需要 CI，可补 `@playwright/test` 与 mock 后端夹具，把本轮浏览器验收固化为自动化测试。
- 用真实样例文件跑完整上传、解析、批改、结果链路；这一步依赖模型 key 与后端 LLM 调用稳定性。
- 继续推进 `BACKEND_TODOS_CN.md` 中的用户级 KB、持久化、OCR、LMS/LTI 等后端阶段事项。
