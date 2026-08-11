# AWS From Idea to Frontier 报名 Demo 设计

## 已确认目标与诚实边界

报名入口要让评审在 90 秒内理解 SmarTAI 的价值，也要允许其继续验证真实技术链路。宣传体验和真实运行必须明确分层：

1. `/frontier` 是无需登录的动态宣传页。允许使用明确标注的合成数据、动画和预计算 walkthrough，但视觉、术语和交互必须与真实产品一致。
2. `/frontier/enter` 从后端取得免密码短时 Demo task scope，随后进入 `/frontier/live` 真实运行入口。输入文件为合成材料，但任务创建、题目识别、图片 OCR、学生作答识别和大模型批改全部调用真实 SmarTAI API。
3. 真实运行失败时显示与安全原因码对应的中英文可操作提示；内部错误码不直接暴露给普通访客。用户可主动返回或打开明确标注的预计算 walkthrough；不得静默降级或把预计算分数冒充为本次运行结果。
4. 页面、URL、仓库和 fixture 中不得出现账号密码、共享模型 Token 或 API Key。后端为每次入口签发随机独立、无 refresh cookie 的短时 scope，Gemini Key 只保存在后端。
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
- 无评分、无红色批注的合成手写 PNG（PDF 副本只用于展示）；
- 混合排版/代码作答 PDF；
- 具名合成扫描 PNG（PDF 副本只用于展示）；
- 只包含上述 raw 学生文件的确定性 ZIP；
- 根目录中的文件 SHA-256 manifest 与 `SHA256SUMS`（覆盖 live 子目录）。

`question_source.pdf`、`DEMO-001_typeset.pdf` 与 `DEMO-001_typeset_raw.pdf` 必须由仓库中的真实 LaTeX 源文件编译生成；生成脚本以 `SOURCE_DATE_EPOCH` 固定元数据。其他扫描/批注资产由确定性 ReportLab/Pillow 生成。

Live raw 学生文件中不得出现 `REVIEW SIGNAL`、建议分数、OCR 更正提示、红色教师批注或答案标签泄漏。四份材料均使用固定的虚构姓名与 `DEMO-001` 至 `DEMO-004` 编号，并同时写在文件名和原件页眉中，避免身份识别失败。排版与混排材料以 PDF 上传；手写与扫描材料以原始 PNG 上传并直接走视觉 OCR，PDF 副本仅保留作展示资产。

## 页面与交互

### 宣传入口 `/frontier`

- 中英文均可切换，以全球市场叙事呈现并遵循用户已保存的界面语言。
- 主题来自批改现场：纸张、石墨、蓝墨水、批注色，而不是通用 AI 霓虹。
- Hero 上半区集中呈现价值主张、真实 Demo CTA 与可信边界；四阶段 walkthrough 独占下一整行，避免把产品演示挤在狭窄右栏。
- 四阶段演示为 `Source → Recognize → Grade → Analyze`：分别突出完整原件输入、原件与结构化识别结果并排复核、rubric 与代码测试支持的初评、基于当前评分结果的班级洞察和新图表生成，不能只替换同一张卡片的文案。
- 当前后端不提供逐字符 OCR 候选、逐字符置信度或精确区域坐标，因此宣传页不得展示 `μ/u` 候选选择器、虚构的字符置信度或暗示精确框选映射；只展示真实存在的“完整原件 + 可编辑结构化文本 + 教师确认”闭环。
- 分析示例只能使用当前 Analytics Agent 实际收到的评分数据，并限定在已支持的 `bar`、`scatter`、`pie`、`histogram`、`box` 图表类型内；没有上下文证据时不得声称分析了隐藏测试、原文件格式或 rubric 来源。
- 视觉文案遵循动作边界：系统负责“识别、建议、标记”，教师负责“复核、修改、决定”；不得把教师介入描述成系统自动代办。
- Stage 04 使用明亮、等高的双面板并用散点图、箱线图和饼图体现不同分析问题；中英文大标题采用短语级换行，避免拆开词组或产生孤行。
- 主 CTA：`Enter the live demo`，经 `/frontier/enter` 自动签发会话后进入 `/frontier/live`。
- 页面所有动画结果都标注为 `Product walkthrough` / `Synthetic content`。
- 关键事实：复杂计算、推导、证明与编程等广泛理工作答，traceable evidence，teacher in control；“混合题型”只用于展示 fixture 的覆盖面，不作为产品核心定位。
- 原文件对照之后设置独立的 `Ask SmarTAI` 双语互动区；可用明确标注的合成 walkthrough 演示自然语言追问、答案依据和与问题对应的新图表，但不得把预计算图表描述成本次 Live 运行结果。

### 真实入口 `/frontier/live`

一次完整运行：

1. `POST /tasks/` 创建独立教师任务并保存 task ID。
2. 对 raw 题目 PDF 执行 source preflight，再调用 `/tasks/{id}/question-preparation/jobs` 完整题目准备入口，轮询真实 job/status/progress。
3. 展示本次真实生成的题干、标答、评分依据与编程题材料，等待评审显式点击确认；不得注入预设 rubric/reference，不得用 fixture 关键词或语义锚点检验模型措辞，也不得用已知 fixture 题干覆盖真实识别文本。固定 Demo 只按唯一 Q1–Q4 题号排序；题号缺失或重复时停止并要求人工检查。
4. 上传 raw 学生作业 ZIP 到 `/tasks/{id}/parse_submissions`，真实处理排版 PDF、图片和手写 OCR。
5. 从后端共享池的已启用 provider 中选择一个，强制 single provider / one sample，并按当前界面语言写入 `feedback_language` 后保存 grading setup。
6. 调用 `/tasks/{id}/grade`，轮询到真实 `graded` 状态。
7. 进入现有 Review/Results 页面，由教师检查来源、调整分数并决定是否发布。

Live 页显示 task ID、job ID、后端 current step、最新 progress event、provider 名称、耗时和真实错误。刷新后可通过 `taskId` 查询参数恢复后端状态；重复运行会显式创建新任务。

Demo 会话进入正式产品工作流后采用固定、可检查的样例桥接：新建/编辑任务页只读显示示例名称、课程、题型与数据范围；“上传题目”页只显示 `question_source.pdf`、四题摘要、原文件入口和返回同一 `taskId` 的 Live CTA；“上传学生作答”页只显示四份具名预置样本、原件入口和同一 Live CTA。Demo 页面不显示自定义文件选择器或元数据编辑控件；普通教师任务保留完整编辑、上传和身份匹配能力。两个 CTA 都由 Live 编排器真实上传文件并继续统一题目准备或 OCR，不用脚本伪造浏览器文件选择。这里的 UI 锁定不是后端 fixture 白名单，当前 scope 风险仍以安全边界章节为准。批改完成后的即时分析只使用本次 `getTaskResult` 数据，并以可暂停、可手动切换、尊重 reduced-motion 的图表轮播展示学生折线、成绩直方图、逐题散点和复核信号环图。

## 安全与成本门

- `/auth/frontier-demo-session` 只在显式启用 Demo、共享池和后端 Gemini Key 时签发随机独立 owner 的短时 `frontier_demo` scope；无密码、无 refresh cookie，且有单进程每日签发上限与冷却。
- 该 scope 只被 task router 接受，普通 experts/courses/admin 教师面继续拒绝。为节约截止前实现成本，当前未做 fixture 上传白名单或完整 demo role，因此它仍可调用整个 task API；独立 Demo 环境只能存放合成数据，不能描述为完整权限沙箱。
- 截止前采用独立 Demo 环境、合成固定 fixtures、单 provider、单 sample；页面不提供共享 API Key，模型密钥只配置在后端环境变量。
- `_GuardedSharedProvider` 必须同时限制文本 `ainvoke` 和视觉 `ainvoke_vision`；共享池关闭时两者都 fail closed。
- 共享池现有额度仍是单进程内存计数，不具备跨 worker 原子结算与平台总金额熔断，因此不得把当前版本当作无登录公共模型服务。
- 当前签发计数和共享池额度均是单进程内存态，多 worker/重启不共享。若从报名 Demo 扩大为长期公共服务，应补 fixture SHA allowlist、持久原子额度、全局预算 kill switch 和更窄的 task capability。

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
- Utility：`SFMono-Regular` / `IBM Plex Mono`，用于 task/job ID、评分证据与测试结果。
- 支持键盘焦点、390px 移动端和 `prefers-reduced-motion`。

## 验收与冻结

- 同一 SHA 通过 TypeScript、Vitest、后端聚焦测试、visible-scope audit 和 production build。
- raw ZIP 可解压，manifest SHA 全部一致，PDF 可逐页渲染，raw 学生材料通过标签泄漏扫描。
- 无痕浏览器从 `/frontier` → `/frontier/enter` → `/frontier/live`，且浏览器网络与存储中不出现 Gemini API Key 明文。
- 用真实 Demo provider 至少跑通一次完整 E2E，保存 task/job ID、运行时间和真实结果截图；未跑通前状态只能写 `unverified`。
- 验证后冻结 commit SHA，并让 Cloudflare 前端与 Demo 后端都固定该 SHA、关闭自动部署。
- 发布前继续遵守 `docs/active_beta_launch/20260803_leader_replan/PRE_PRODUCTION_SECURITY_RELEASE_GATE_CN.md`；真实学生数据仍为 No-Go。
