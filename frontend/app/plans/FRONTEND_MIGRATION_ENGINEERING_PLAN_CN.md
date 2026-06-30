# Vite React 前端迁移工程阶段与多 Agent 协作计划

更新时间：2026-06-30
目标分支：`codex/vite-react-frontend`
范围：完成 SmarTAI 当前教师端批改主流程的 React 迁移，并保留未来 LMS/用户级知识库扩展边界。

## 1. 总体策略

迁移采用“先搭骨架，再迁主链路，再打磨结果区，再验收收口”的方式。不能一开始让多个 Agent 同时写同一套核心状态和布局，否则很容易出现重复 API client、重复状态模型、重复组件样式。

推荐并行方式：

- 阶段 0 到阶段 2 有明确先后顺序，必须串行打底。
- 阶段 3 开始可以拆成多个相对独立的 Agent 并行。
- 每个 Agent 只拥有一个功能域，不能跨域重写共享 API、AppShell、主题 token。
- 合并顺序由平台/架构 Agent 控制，避免互相覆盖。

## 2. 阶段拆分

### Phase 0 - 文档与边界冻结

状态：已完成

目标：

- 保存产品范围文档。
- 保存工程分工文档。
- 明确当前只展示教师端主流程。
- 明确 LMS、课程作业、学生端不展示。

产出：

- `REACT_REWRITE_STATUS_CN.md`
- `FRONTEND_MIGRATION_ENGINEERING_PLAN_CN.md`

是否可并行：否。必须先完成，作为后续所有 Agent 的共同合同。

### Phase 1 - React 工程脚手架与设计系统

负责人建议：Agent A - Platform/UI Foundation

目标：

- 在 `frontend/app` 创建 Vite React + TypeScript 工程。
- 配置 Tailwind、CSS variables、light/dark/system 主题。
- 配置 Router、QueryClient、Toast、ErrorBoundary。
- 建立 AppShell、教师侧边栏、顶部栏、移动端导航。
- 建立基础组件：Button、IconButton、Input、Select、Switch、Tabs、Table、Dialog、Drawer、Card、EmptyState、StatTile、UploadDropzone。

关键约束：

- 侧边栏只展示教师主流程入口。
- 不出现 LMS/学生端/课程作业导航。
- 颜色不能做单一蓝紫主题；使用 slate/indigo/teal/amber/red 的克制组合。
- 卡片半径不超过 8px。

是否可并行：基本不可并行。它是所有页面的基础。

验收：

- 空 App 可运行。
- `/login`、受保护 layout、404 页面可显示。
- 深浅主题可切换并持久化。
- 中英文 UI provider 可切换并持久化。

### Phase 2 - API Client、类型与状态合同

负责人建议：Agent B - API/Data Contract

目标：

- 手写或生成前端 TypeScript 类型。
- 实现统一 API client：JSON、multipart upload、错误处理、auth header。
- 实现 hooks：auth、tasks、kb、experts、analytics、health。
- 实现 task progress polling：轮询 `/tasks/{id}/state`，终态刷新详情和结果。
- 预留隐藏的 courses/assignments/lms 类型，但不注册页面。

关键约束：

- grade payload 不暴露语言选择；语言相关字段集中在一个 API 封装点。
- 上传 API 支持浏览器上传进度。
- 后端冷启动/网络错误有统一中文提示。
- API 层不能把 LMS 能力暴露到导航或页面。

是否可并行：可与 Phase 1 后半段小范围并行，但必须等路由和 provider 结构确定后合并。

验收：

- auth token 可以存取。
- mock API 下 task list、task detail、progress polling 可跑。
- API 类型能覆盖当前教师主流程。

### Phase 3 - 教师主流程页面迁移

负责人建议：多个 Agent 并行，按子域拆分

此阶段可以并行，但每个 Agent 必须只改自己的 feature 目录，公共组件需求先提给 Agent A。

#### Agent C - Auth 与教师 Shell

任务：

- 登录页。
- 注册申请页。
- 角色守卫。
- student 角色进入“学生端暂未开放”提示页。
- 教师 AppShell 导航。

依赖：

- Phase 1 AppShell 基础。
- Phase 2 auth API。

禁止：

- 不做学生 Dashboard。
- 不加课程/LMS 入口。

#### Agent D - Task Setup 与知识库

任务：

- New Task。
- Setup 批改配置页。
- task-scoped KB 上传、列表、删除。
- BYOK 专家选择读取。
- 批改风格、质量保证设置。

依赖：

- Phase 2 tasks/kb/experts API。

关键点：

- 不展示“学科语言”或“评语语言”。
- 知识库文案必须说清“本任务资料”，不能说“我的全局知识库”。

#### Agent E - 上传与预批改审阅

任务：

- 上传题目。
- 题目预览与编辑。
- 上传学生作答。
- 学生列表。
- 学生作答详情与编辑。
- 开始批改按钮与进度卡。

依赖：

- Phase 2 task upload/polling API。
- Phase 1 UploadDropzone、Table、Dialog。

关键点：

- 上传完成后自动跳转下一步。
- 允许用户离开页面后恢复进度。
- 保持 task-centric workflow。

#### Agent F - 结果工作区

任务：

- ResultsLayout。
- 结果总览。
- 按题分析。
- 可视化。
- 学生详情。
- 题目详情。
- 学生/题目之间的交叉跳转。
- NL filter/summary/chart。

依赖：

- Phase 2 analytics API。
- Phase 1 Tabs/Table/Plotly wrapper。

关键点：

- 进入学生详情或题目详情后仍能看到结果区导航。
- 用户不需要回到结果首页才能切换总览/按题/可视化。
- 低置信、专家详情、教师评语、AI 易错点都要保留。

#### Agent G - BYOK Experts 与 Settings

任务：

- BYOK Experts 列表、添加、启停、删除。
- Settings 后端健康检查。
- Settings 中语言/主题偏好。
- Account 退出。

依赖：

- Phase 2 experts/health API。

关键点：

- API key 输入不持久显示。
- 不新增共享池配置入口。

是否可并行：Phase 3 内部可并行。推荐 C/D/E/F/G 同时做，但每天由 Agent A/B 检查公共接口冲突。

### Phase 4 - 集成、隐藏项审计与体验打磨

负责人建议：Agent H - Integration/QA

目标：

- 统一页面间跳转。
- 统一 loading、empty、error、toast。
- 移动端布局检查。
- 深浅主题对比检查。
- 中英文 UI 文案补齐。
- 禁止展示项审计。

必须审计：

- Router 中没有 LMS/课程/作业/学生端可用入口。
- Sidebar/Topbar 没有隐藏能力的链接。
- 空状态文案没有暗示 LMS 已接入。
- Setup 中没有批改语言选择。
- 知识库文案没有夸大为用户级全局 KB。

是否可并行：可以与 Phase 3 后半段滚动进行，但最终收口必须串行。

### Phase 5 - 自动化测试与本地验收

负责人建议：Agent I - Test/Verification

目标：

- 补类型检查、lint、build。
- Playwright mock 后端验收教师主流程。
- 截图检查桌面和移动端。
- 文档状态勾选同步。

测试场景：

- 登录成功进入教师端。
- 注册页存在但不能进入开放注册流程。
- student 角色不展示学生端功能。
- Dashboard 创建任务。
- Setup 开关 KB，并上传/删除 task KB。
- 上传题目后进入题目预览。
- 编辑题干/评分标准。
- 上传作答后进入学生预览。
- 编辑学生作答。
- 开始批改并轮询进度。
- 批改完成进入结果。
- 结果总览、按题、可视化、学生详情、题目详情之间可直接切换。
- 深浅主题和中英文切换刷新后保持。

是否可并行：可在 Phase 3 稳定后开始写测试，最终验收必须在所有功能合并后串行跑。

## 3. 推荐 Agent 分工表

| Agent | 负责范围 | 可并行性 | 主要产出 | 禁止事项 |
|---|---|---:|---|---|
| A Platform/UI Foundation | 工程脚手架、设计系统、AppShell | 先行 | 基础工程、组件、主题 | 不写业务页面深逻辑 |
| B API/Data Contract | API client、类型、query hooks | 先行/半并行 | API 层、类型层 | 不写页面 UI |
| C Auth/Shell | 登录、注册、角色守卫 | Phase 3 并行 | Auth 页面与守卫 | 不做学生工作台 |
| D Setup/KB | Setup、task KB、专家选择读取 | Phase 3 并行 | 配置流程 | 不声称全局知识库已完成 |
| E Upload/Review | 上传题目/作答、预览编辑、批改启动 | Phase 3 并行 | 主批改前链路 | 不改结果区 |
| F Results/Analytics | 结果工作区、详情导航、图表/NL | Phase 3 并行 | 结果体验 | 不改上传链路 |
| G Experts/Settings | BYOK、Settings、偏好 | Phase 3 并行 | 专家与设置页 | 不加共享池管理 |
| H Integration/QA | 整合、隐藏项审计、视觉打磨 | 后期滚动 | 集成修复 | 不大改架构 |
| I Tests | 测试与验收 | 后期滚动 | Playwright、build 验收 | 不改产品范围 |

## 4. 合并顺序

推荐顺序：

1. 合并 Agent A 的脚手架与设计系统。
2. 合并 Agent B 的 API 与类型。
3. 合并 Agent C 的 Auth 与守卫。
4. 并行合并 Agent D/E/G 的教师流程外围页面。
5. 合并 Agent F 的结果工作区。
6. Agent H 做统一交互和隐藏项审计。
7. Agent I 跑完整测试并更新状态文档。

理由：

- Auth 和 API 是所有页面依赖。
- 上传链路和结果链路可以分开做，但最终需要共享 task 状态模型。
- 结果工作区复杂，最好在任务数据结构稳定后合并。
- LMS/学生端隐藏审计必须放在后期，因为中途可能有人为了测试加临时入口。

## 5. 协作规则

- 每个 Agent 开工前先读 `REACT_REWRITE_STATUS_CN.md`。
- 每个 Agent 完成一项后勾选状态文档对应清单。
- 公共组件新增由 Agent A 审核命名和样式。
- API 类型新增由 Agent B 审核字段与后端一致性。
- 不允许在业务页面里直接写裸 `fetch`。
- 不允许新增可见 LMS、课程作业或学生端入口。
- 不允许在 Setup 展示批改语言选择。
- 所有“后端后续要改”的发现都写入状态文档的后端 TODO，不散落在页面注释里。

## 6. 风险与控制

| 风险 | 影响 | 控制 |
|---|---|---|
| 多 Agent 重复实现 API client | 状态混乱 | Phase 2 统一 API 层先合并 |
| 结果页继续变成孤岛 | 用户体验不达标 | Phase 3F 必须做 ResultsLayout |
| LMS 预留误展示 | 用户误以为已接入 | Phase 4 做隐藏项审计 |
| 知识库能力被夸大 | 产品承诺不准确 | 文案只说 task-scoped KB |
| 后端语言仍默认英文 | 前端体验与目标不一致 | 前端隐藏语言选择，后端 TODO 记录 |
| 主题/i18n 后补成本高 | 文案和样式返工 | Phase 1 就接入 provider |
| Reflex 与 React 部署混淆 | 本地/线上难排错 | README 明确两个前端并存和启动方式 |

## 7. 里程碑验收

### M1 - Shell 可运行

- [ ] React dev server 启动
- [ ] 登录页可见
- [ ] 教师 AppShell 可见
- [ ] 主题/i18n 可切换

### M2 - 主流程可走通到批改前

- [ ] 创建任务
- [ ] Setup 配置
- [ ] 上传题目并查看编辑
- [ ] 上传作答并查看编辑

### M3 - 批改与结果可用

- [ ] 启动批改
- [ ] 进度轮询
- [ ] 批改完成跳结果
- [ ] 结果五个视图可直接切换

### M4 - 收口可交付

- [ ] 禁止展示项审计通过
- [ ] typecheck/lint/build 通过
- [ ] Playwright 关键流通过
- [ ] 文档勾选状态与代码一致
