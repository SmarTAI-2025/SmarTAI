# AWS From Idea to Frontier 报名 Demo 设计

## 已确认目标与诚实边界

报名入口要让评审在 90 秒内理解 SmarTAI 的价值，也要允许其继续验证真实技术链路。宣传体验和真实运行必须明确分层：

1. `/frontier` 是无需登录的动态宣传页。允许使用明确标注的合成数据、动画和预计算 walkthrough，但视觉、术语和交互必须与真实产品一致。
2. `/frontier/live` 是登录后的真实运行入口。输入文件为合成材料，但任务创建、题目识别、图片 OCR、学生作答识别和大模型批改全部调用真实 SmarTAI API。
3. 真实运行失败时显示真实错误码和后端消息，再由用户主动选择 `View precomputed walkthrough`；不得静默降级或把预计算分数冒充为本次运行结果。
4. 页面、URL、仓库和 fixture 中不得出现账号密码、共享模型 Token 或 API Key。每位评审使用独立、短期的教师账号，凭据只通过私密渠道发送且不得多人复用。
5. 当前后端不持久化可下载的原文件字节；Live 页只能如实说明预览的是“本次上传所用的同一浏览器侧合成副本”，不得宣称是服务端保存的原件。

## 隔离分支与代码纳入

- 独立分支：`codex/aws-frontier-demo-20260812`。
- 独立 worktree：`/private/tmp/SmarTAI-aws-frontier-demo`。
- 基线：`origin/main@6f7ac33d28c9520d7a88fbc5f219eaa9044c3ef5`。
- 复用：基线已合入的 React 产品 UI、任务 API、任务状态轮询、评分设置、错误语义和真实后端工作流。
- 单独抽取：原文件 Preview 面板、触发器、可拖动双栏工作区和对应文案。
- 排除整栈合并：PR #17/#18/#21/#23/#24/#25/#26；其功能可在截止后按正常评审顺序进入 `main`。
- 明确排除：PR #15 中任何明文账号、密码或带答案提示的 raw fixture。
- 冻结后，前后端 Demo 部署都固定到同一个审核 SHA，关闭自动跟随 `main`。

## 两套严格分离的合成资产

课程：`Mathematical Methods & Scientific Programming`

题目集共 4 题、30 分：

1. 微积分：`∫₀¹ x e^(x²) dx`，5 分。
2. 力学：粗糙斜面上的 2 kg 滑块，8 分。
3. 线性代数：证明 `ker(A) = ker(AᵀA)` 并推出秩相等，7 分。
4. 编程：实现数值稳定的 `stable_softmax`，10 分。

`public/frontier-demo/` 中的 annotated 文件仅供宣传动画与预计算 walkthrough 使用，可以出现批注、提示和预计算分数。

`public/frontier-demo/live/` 中的 raw 文件只用于真实 API：

- 原始题目 PDF；
- 教师 rubric/reference PDF；
- 无评分、无红色批注、无 OCR 提示的排版作答 PDF；
- 无评分、无红色批注的合成手写 PNG/PDF；
- 混合排版/代码作答 PDF；
- 匿名扫描件；
- 只包含上述 raw 学生文件的确定性 ZIP；
- 根目录中的文件 SHA-256 manifest 与 `SHA256SUMS`（覆盖 live 子目录）。

`question_source.pdf`、`DEMO-001_typeset.pdf` 与 `DEMO-001_typeset_raw.pdf` 必须由仓库中的真实 LaTeX 源文件编译生成；生成脚本以 `SOURCE_DATE_EPOCH` 固定元数据。其他扫描/批注资产由确定性 ReportLab/Pillow 生成。

Live raw 学生文件中不得出现 `REVIEW SIGNAL`、建议分数、OCR 更正提示、红色教师批注或答案标签泄漏。所有身份使用 `DEMO-001` 等虚构编号。

## 页面与交互

### 宣传入口 `/frontier`

- 英文默认，以全球市场叙事呈现。
- 主题来自批改现场：纸张、石墨、蓝墨水、批注色，而不是通用 AI 霓虹。
- Hero 编排动画演示 `Source → Recognize → Grade → Decide`。
- 主 CTA：`Enter the live demo`，登录后回到 `/frontier/live`。
- 页面所有动画结果都标注为 `Product walkthrough` / `Synthetic content`。
- 关键事实：mixed STEM、traceable evidence、teacher in control。

### 真实入口 `/frontier/live`

一次完整运行：

1. `POST /tasks/` 创建独立教师任务并保存 task ID。
2. 上传 raw 题目 PDF 到 `/tasks/{id}/extract_problems`，轮询真实 job/status/progress。
3. 先以题号和稳定语义锚点逐题校验识别结果；只有四题都唯一匹配时，才写入合成教师 rubric、参考答案、分值和编程测试。数量相同但错序、错拆或题干错配也必须停止。
4. 上传 raw 学生作业 ZIP 到 `/tasks/{id}/parse_submissions`，真实处理排版 PDF、图片和手写 OCR。
5. 从当前账号的已启用 provider 中选择一个，强制 single provider / one sample，保存 grading setup。
6. 调用 `/tasks/{id}/grade`，轮询到真实 `graded` 状态。
7. 进入现有 Review/Results 页面，由教师检查来源、调整分数并决定是否发布。

Live 页显示 task ID、job ID、后端 current step、最新 progress event、provider 名称、耗时和真实错误。刷新后可通过 `taskId` 查询参数恢复后端状态；重复运行会显式创建新任务。

## 安全与成本门

- 不公开或复用共享账号；同一 owner 会看到同一任务空间，并可删除、重跑和消耗模型额度。每位评审单独创建短期账号，演示结束后撤销。
- 当前没有受限的 `demo role`；评审登录的是普通教师账号。因此独立 Demo 环境只能存放合成数据，关闭公开注册，报名链接直达固定 fixture 流程，不能把它描述为权限沙箱。
- 截止前采用独立 Demo 环境、合成固定 fixtures、单 provider、单 sample；页面不提供共享 API Key，邀请账号也不预置可见密钥。
- `_GuardedSharedProvider` 必须同时限制文本 `ainvoke` 和视觉 `ainvoke_vision`；共享池关闭时两者都 fail closed。
- 共享池现有额度仍是单进程内存计数，不具备跨 worker 原子结算与平台总金额熔断，因此不得把当前版本当作无登录公共模型服务。
- 若未来开放匿名公共 Live Demo，应另建受限 `/demo/session`/`demo/runs`：fixture SHA allowlist、独立 demo 身份、并发 1、持久原子额度、全局预算 kill switch，且禁止访问普通任务和 provider 管理接口。

## 视觉系统

| Token | Hex | 用途 |
|---|---:|---|
| Paper | `#F7F8F4` | 宣传页背景与原稿空间 |
| Graphite | `#17202A` | 标题与主体文字 |
| Cobalt Ink | `#2457D6` | 主行动与结构化识别 |
| Vermilion Mark | `#D94A3A` | 需要教师复核的批注 |
| Sage Proof | `#DCE9DE` | 已验证证据 |
| Rule | `#D7D9D2` | 纸张分隔线 |

- Display：`Iowan Old Style` / `Georgia`，只用于宣传页论文式标题。
- Body：沿用真实产品字体、布局尺度和组件语言。
- Utility：`SFMono-Regular` / `IBM Plex Mono`，用于 task/job ID、置信度与测试结果。
- 支持键盘焦点、390px 移动端和 `prefers-reduced-motion`。

## 验收与冻结

- 同一 SHA 通过 TypeScript、Vitest、后端聚焦测试、visible-scope audit 和 production build。
- raw ZIP 可解压，manifest SHA 全部一致，PDF 可逐页渲染，raw 学生材料通过标签泄漏扫描。
- 无痕浏览器从 `/frontier` → 登录 → `/frontier/live`；登录后 return path 不允许跨域跳转。
- 用真实 Demo provider 至少跑通一次完整 E2E，保存 task/job ID、运行时间和真实结果截图；未跑通前状态只能写 `unverified`。
- 验证后冻结 commit SHA，并让 Cloudflare 前端与 Demo 后端都固定该 SHA、关闭自动部署。
- 发布前继续遵守 `docs/active_beta_launch/20260803_leader_replan/PRE_PRODUCTION_SECURITY_RELEASE_GATE_CN.md`；真实学生数据仍为 No-Go。
