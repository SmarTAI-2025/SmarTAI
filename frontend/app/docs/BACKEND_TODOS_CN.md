# React 前端重构相关后端 TODO

更新时间：2026-07-01 约20:20 CST
适用范围：记录 React 前端重构过程中发现但本次不实现的后端事项，避免散落在页面注释里。

## 1. 用户级可复用知识库

当前状态：

- 后端已有 task-scoped KB，任务 Setup 可上传、查看、删除本任务资料。
- RAG 存储仍是内存态，重启会丢。
- React 当前已有用户级知识库前端先行入口：侧边栏 `/knowledge-base`、本地资料清单、Setup 选择器，以及“上传本任务资料时同时加入个人清单”的勾选项。
- 上述前端能力只保存浏览器 localStorage 中的元数据和任务选择关系；不会保存文件内容，也不会让用户级资料真正参与当前批改。

后端需要：

- 增加 user-scoped KB 数据模型。
- 增加用户级 KB 上传、列表、删除 API。
- 增加任务配置字段，记录本任务引用哪些用户级 KB 文档。
- 增加“把用户级 KB 文档引用/复制到 task-scoped RAG”的后端语义，避免前端选择后实际不生效。
- 批改时同时支持 task-scoped KB 与 user-scoped KB 检索范围。
- 明确持久化方案，至少 Stage 2 前落 PostgreSQL/对象存储或等效方案。

原因：

- 当前能力不能被描述为“我的全局知识库”或“跨任务复用资料库”。
- 当前前端入口必须继续明确“前端先行、后端待接入”，直到上述 API 和检索语义完成。

## 2. 批改语言自动识别

当前状态：

- React 前端不展示批改语言选择。
- API client 仍集中传兼容字段，避免页面到处知道 `GradeRequest.language`。
- 后端尚未完成真正的“基于题目/作答自动决定反馈语言”策略。

后端需要：

- 调整 `GradeRequest.language` 默认值与兼容策略。
- 调整 `backend/skills/base.py` 的 `build_system_prompt` 语言指令。
- 让各 skill 基于题面、学生作答、教师评分标准自动生成合适语言反馈，必要时双语。
- 确认旧客户端传 `"en"` 或其他历史值时不会强制错误语言输出。

原因：

- 用户希望批改语言由 LLM 自动识别，而不是前端配置项。
- 在后端完成前，React 只能隐藏语言控件，不能声称已实现自动识别。

## 3. LMS / LTI / 课程作业

当前状态：

- React 只保留隐藏类型 `src/types/lms.ts`。
- 没有注册 LMS/课程/作业路由。
- Sidebar、Topbar、教师主流程不链接 LMS 能力。

后端需要：

- LTI 1.3 launch / OIDC 登录。
- 外部课程映射 `ExternalCourseMapping`。
- 外部作业映射 `ExternalAssignmentMapping`。
- LMS 用户角色映射 teacher / TA / student。
- LMS 提交文件读取接口。
- 成绩与反馈回传接口。
- 审计日志与错误重试。
- 与现有 JWT/角色系统、TaskStore、未来持久化层的权限边界设计。

原因：

- 当前阶段产品边界是教师端批改主流程。
- 提前展示 LMS 会误导用户以为已可接入学校系统。

## 4. 共享模型池额度与安全

当前状态：

- BYOK 是当前前端主要模型供给入口。
- React 没有新增共享池管理入口。
- 后端文档要求公开共享 env key 前必须加硬上限。

后端需要：

- 每用户每日 token 上限、次数上限、累计总上限。
- 共享池强制单专家、单采样。
- 免费池队列与更紧的 RPM 限流。
- 一键熔断和预算告警。
- 额度计数初期可内存，Stage 2 应落 DB/Redis。

原因：

- 没有限额时，共享池 key 公开给测试者会有成本失控风险。
- 这些控制必须在后端强制，不能只靠前端 UI。

## 5. 持久化与对象存储

当前状态：

- TaskStore、JobStore、BYOK registry、task KB 仍以内存为主。
- React 可以展示当前任务数据，但不能解决重启丢失。

后端需要：

- PostgreSQL 持久化用户、任务、题目、学生、结果、额度计数。
- 对象存储保存学生作业原件、PDF、图片、导出报告。
- DB 层 owner/course 权限约束与审计日志。
- 迁移后保证 `/tasks/*` 与 `/analytics/*` API 对前端保持兼容。

原因：

- Stage 1 可接受内存态和脱敏样例数据；Stage 2 正式推广前必须持久化。

## 6. OCR / 图片与扫描件摄入

当前状态：

- OCR/vision ingestion 未实现。
- React 上传页只能说明图片题面和手写 OCR 是后续接入项。

后端需要：

- 设计 `OCRProvider` 抽象。
- 支持 LLM vision 或 Mathpix 等 provider，把图片/扫描页转为含 LaTeX 的文本。
- 将 OCR 结果接入现有人工订正闭环。
- 上传 API 与文件处理层放开图片/扫描 PDF 的可用路径。

原因：

- 数理作业的手写/拍照输入是产品关键缺口，但不能由前端文案提前承诺。
