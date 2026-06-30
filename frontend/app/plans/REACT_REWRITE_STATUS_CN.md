# SmarTAI Vite React 前端重构状态总表

更新时间：2026-06-30
分支：`codex/vite-react-frontend`
范围：仅前端迁移文档与后续 React App 计划；当前不改后端。

## 1. 已确认产品边界

本次 React 重构先做登录后的教师端批改工作台。未来 LMS、课程作业、学生端等方向可以在类型和 API 层预留，但不能出现在当前用户使用路径里。

### 当前必须展示

- 登录页 `/login`
- 注册申请页 `/register`，保持“注册暂未开放/需邀请”的产品语义
- 教师 Dashboard
- History
- New Task
- 批改配置 Setup
- 上传题目
- 题目预览与题干/评分标准编辑
- 上传学生作答
- 学生作答预览与识别结果编辑
- 开始批改与进度轮询
- 批改结果总览
- 按题分析
- 可视化
- 学生批改详情
- 单题全班详情
- BYOK Experts
- Settings

### 当前可以展示但必须限制语义

- 知识库入口只能作为“本次批改配置”的一部分展示。
- 当前后端真实能力是 task-scoped KB：每个任务上传、查看、删除参考资料，并在本次批改中选择是否启用。
- 不能把当前能力描述成“每个用户可复用的全局个人知识库”。用户级可复用 KB 是后端后续事项。

### 当前禁止展示

- 学生端工作台
- 课程管理
- 作业发布
- 学生提交
- 学生成绩页
- LMS / LTI / SSO / 成绩回传入口
- 任何 Canvas、Moodle、学校 LMS 连接配置入口
- 任何会让用户以为已经完成 LMS 接入的页面、按钮、导航项、空状态文案

如果为后续开发写了这些模块的类型或 API client，必须保持隐藏：不注册路由、不进导航、不从教师主流程链接过去。

## 2. 批改语言与 UI 语言

- 全站 UI 语言：前端实现中英文切换，默认 `zh-CN`，可切到 `en-US`。
- 批改语言：前端不再让用户选择，不在 Setup 中展示“学科语言”或“评语语言”。
- 后端当前 `GradeRequest.language` 仍存在，React client 需要集中封装；后续后端改成自动识别后，只改 API 层。
- LLM 自动识别批改语言属于后端后续事项，前端不声称已经实现。

## 3. 功能状态矩阵

| 功能 | 代码状态 | 是否展示 | 是否依赖后端新改动 | 备注 |
|---|---|---:|---:|---|
| Vite React App 工程 | 骨架已完成 | 是 | 否 | `frontend/app` 可 typecheck/build |
| 教师登录 | 静态骨架 | 是 | 否 | API hooks 已有，页面尚未真实提交登录 |
| 注册申请页 | 静态骨架 | 是 | 否 | 保持当前“暂未开放”语义 |
| 教师 Dashboard | 静态骨架 | 是 | 否 | 仅展示教师任务，任务数据待接入 |
| New Task / History | 静态骨架 | 是 | 否 | API hooks 已有，页面待接入 `/tasks/*` |
| Setup 批改配置 | 静态骨架 | 是 | 部分否 | 不展示批改语言选择 |
| task-scoped KB 上传/删除 | API 已封装，UI 骨架 | 是 | 否 | 复用 `/tasks/{id}/kb`，上传交互待接入 |
| 用户级全局知识库 | 未开始 | 否 | 是 | 后端 TODO |
| 上传题目与题目编辑 | API 已封装，UI 骨架 | 是 | 否 | 上传/编辑交互待接入 |
| 上传作答与作答编辑 | API 已封装，UI 骨架 | 是 | 否 | 上传/编辑交互待接入 |
| 批改启动与进度 | API 已封装，hook 已有 | 是 | 否 | 轮询 `/tasks/{id}/state`，页面待接入 |
| 结果总览 | 静态骨架 | 是 | 否 | 学生筛选、低置信、NL summary/filter 待接入 |
| 按题分析 | 静态骨架 | 是 | 否 | 复用 `/analytics/{id}/per_question/{q_id}` 待接入 |
| 结果可视化 | 静态骨架 | 是 | 否 | Plotly + NL chart 待接入 |
| 学生详情 | 静态骨架 | 是 | 否 | 已有持久结果导航结构 |
| 题目详情 | 静态骨架 | 是 | 否 | 已有持久结果导航结构 |
| BYOK Experts | 静态骨架，API 已封装 | 是 | 否 | 列表/添加/删除交互待接入 |
| Settings | 基础可用 | 是 | 否 | 主题、语言已可切换；后端健康检查待接入 |
| 学生端 | 禁止展示 | 否 | 部分是 | 可保留文档，不做入口 |
| 课程/作业 API client | 可预留 | 否 | 否 | 若写代码，必须隐藏 |
| LMS/LTI/SSO 类型 | 已预留隐藏类型 | 否 | 是 | 仅 `types/lms.ts`，不展示 |

## 4. 执行清单

### 文档与范围

- [x] 创建新分支 `codex/vite-react-frontend`
- [x] 保存 React 重构范围计划文档
- [x] 保存工程阶段与 Agent 协作计划文档
- [x] 后续每个阶段完成后更新本状态总表
- [ ] 实现结束前做一次“禁止展示项”审计

### 工程基础

- [x] 创建 `frontend/app` Vite React + TypeScript 工程
- [x] 配置 Tailwind、Radix primitives、lucide、sonner、Plotly、TanStack Query
- [x] 配置 typecheck、build 脚本
- [x] 配置 `.env.example` 和 README

### API 与状态

- [x] 实现统一 API client
- [x] 实现 auth token 与 user LocalStorage
- [x] 实现 task API hooks
- [x] 实现上传进度与 task state 轮询
- [x] 实现 experts API hooks
- [x] 实现 analytics API hooks
- [x] 实现 KB API hooks

### 教师端页面

- [x] 登录/注册静态骨架
- [x] AppShell、教师导航、主题切换、语言切换
- [x] Dashboard / History / New Task 静态骨架
- [x] Setup 静态骨架
- [x] 上传题目 / 题目预览编辑静态骨架
- [x] 上传作答 / 学生作答预览编辑静态骨架
- [x] 批改进度 hook
- [x] 结果总览 / 按题分析 / 可视化静态骨架
- [x] 学生详情 / 题目详情静态骨架
- [x] BYOK Experts 静态骨架
- [x] Settings 主题与语言基础功能

### 验收

- [x] `npm run typecheck`
- [ ] `npm run lint`
- [x] `npm run build`
- [ ] Playwright 覆盖教师主流程
- [ ] 截图检查桌面与移动端布局
- [ ] 文档状态与实际代码一致

## 5. 后端后续 TODO

这些事项不在本次前端迁移中实现，只记录清楚，方便后续拆后端任务。

### 用户级知识库

目标：每个用户可维护自己的可复用知识库，在任务 Setup 中选择是否引用。

后端需要：

- 增加 user-scoped KB 数据模型。
- 增加用户级 KB 上传、列表、删除 API。
- 增加任务配置字段，记录本任务引用哪些用户级 KB 文档。
- 批改时同时支持 task-scoped KB 与 user-scoped KB 检索范围。
- 明确持久化方案，当前内存 RAG 重启会丢。

前端后续展示：

- 可以在 Settings 或独立教师知识库页管理“我的知识库”。
- Setup 中选择“仅本任务资料”或“引用我的知识库资料”。
- 当前阶段不展示这些入口。

### 批改语言自动识别

目标：前端不提供批改语言选项，后端自动根据题目与学生作答决定反馈语言。

后端需要：

- 调整 `GradeRequest.language` 默认与兼容策略。
- 调整 `backend/skills/base.py` 的 `build_system_prompt` 语言指令。
- 让 skill 基于题面/作答语言自动生成反馈，必要时双语。
- 确认旧客户端传 `"en"` 时不会强制英文输出。

前端当前处理：

- 不展示批改语言控件。
- API client 集中封装 grade payload，方便后续一次改完。

### LMS / LTI / 课程作业

目标：后续作为外部工具接入学校 LMS，而不是当前阶段展示给用户。

后端需要：

- LTI 1.3 launch / OIDC 登录。
- 外部课程映射 `ExternalCourseMapping`。
- 外部作业映射 `ExternalAssignmentMapping`。
- LMS 用户角色映射 teacher / TA / student。
- LMS 提交文件读取接口。
- 成绩与反馈回传接口。
- 审计日志与错误重试。

前端当前处理：

- 可以预留类型和隐藏 API client。
- 不注册可访问页面，不进导航。
- 不在空状态、按钮、提示文案中提 LMS 已可用。
