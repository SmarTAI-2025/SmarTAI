# SmarTAI 项目现状与路线图（2026-06-20）

> 本文档基于 2026-06-20 对当前代码库的直接核验编写，取代此前 `docs/ROADMAP.md`（2026-04-26）与 `docs/docs_new/PRODUCT_STATUS_CN0502.md`（2026-05-02）中已过期的状态描述。所有文件链接为仓库根目录相对路径，可点击跳转。
>
> 适用对象：项目维护者 + 即将加入的两位同学（同学1 = 性能/模型/OCR；同学2 = 部署/运维）。配套商业与采购文档见 [docs/GO_TO_MARKET_AND_OPS_CN.md](docs/GO_TO_MARKET_AND_OPS_CN.md)。

---

## 0. 摘要 / TL;DR

- **当前端到端能跑的链路**：教师登录 → 新建任务 → 上传题目（txt/pdf/压缩包）→ 抽题 → 上传学生作答 → 解析 → 多专家批改（含 SymPy 计算校验 + 代码沙箱）→ 结果/学情分析（5 张图 + 自然语言自定义查询）。任务级状态机 + 幂等 + 进度轮询都已就绪。
- **两个最受关注点的明确结论**：
  - 🔴 **OCR 还没做**。手写/拍照/扫描作业目前**进不去系统**——[file_processing.py](backend/tools/file_processing.py) 只提取数字文本和 PDF 内嵌文本，图片被直接丢弃，没有任何 vision / Mathpix / OCR 引擎。这是数理方向的头号缺口。
  - 🟡 **RAG 后端其实做完了**，而且任务级知识库**上传 UI 也在**（在「任务 Setup 页」，受开关控制），并已接入批改（证明题 prompt 注入检索到的教材内容）。你以为"前端看不到"，是因为全局导航里那个 `Knowledge Base` 页还是个写死的占位壳——真正能用的入口在 Setup 页里。
- **公平性 P0**（2026-05-03 批准的三件套）已全部落地：Indecisiveness Score + Minority Veto + 反越狱前缀。
- **持久化仍是纯内存**，进程重启/休眠即丢——这是上规模（Stage 2）前的头号工程长板。
- **部署配置已写好但尚未真正上线**（render.yaml 里三处 URL 还是占位符）。

---

## 1. 代码结构总览

**总体架构**：`FastAPI 后端`（Agent → Skill → Tool 三层）+ `Reflex 前端`（Python 包名 `frontend/smartai_v2/`）。仅 V2；V1 路由已在迁移后清理，[backend/main.py](backend/main.py) 在 `SMARTAI_GRADING_ENGINE != v2` 时直接 `RuntimeError`。

```
backend/
├── main.py            # FastAPI 入口；启动时设代理 env、装配 RAG 检索器、挂载所有 router
├── config.py          # Pydantic Settings：模型默认值/限流/JWT/公平阈值（env 前缀 SMARTAI_）
├── models.py          # 全部 Pydantic 数据模型：Task 状态机、Correction、ProviderConfig…
├── agents/            # 编排层（"做什么"）
│   ├── ingest_agent.py       # 抽题 / 解析学生作答
│   ├── grading_agent.py      # 批改总编排（按题型路由到 skill，逐学生逐题）
│   ├── multi_expert.py       # 多专家/多采样 + 合成 + 公平信号（IS / Minority Veto）
│   └── analytics_agent.py    # 学情分析：filter / summarize / make_chart 三模式
├── skills/            # 能力层（"怎么批某一类题"）
│   ├── base.py               # Skill 基类 + 反越狱系统前缀 + build_system_prompt
│   ├── concept.py            # 概念/简答题
│   ├── calculation.py        # 计算题（4 档 SymPy fallback）
│   ├── proof.py              # 证明/推理题（注入 RAG 教材 context）
│   └── programming.py        # 编程题（4 档代码沙箱 fallback）
├── tools/             # 工具层（"可被复用的原子能力"）
│   ├── file_processing.py    # 压缩包解包 + PDF 文本 + txt 解码（⚠️ 无 OCR）
│   ├── classify.py           # 题型分类
│   ├── numerical.py          # 数值/符号等价校验（SymPy）
│   ├── code_interpreter.py   # 代码执行入口
│   ├── sandbox_runtime.py    # 子进程沙箱 + 全局 Semaphore(8) 并发闸
│   ├── knowledge.py          # RAG 检索接口（retrieve()，可热插 retriever）
│   └── structured_llm.py     # 结构化 LLM 输出（schema 校验）
├── llm/               # 供应商层（BYOK 多专家）
│   ├── providers.py          # BaseProvider + gemini/openai/zhipu/anthropic + RPM 限流器
│   └── registry.py           # ExpertRegistry：注册/列举/选默认专家
├── rag/               # RAG MVP（任务级、内存）
│   ├── chunker.py            # 文档切块
│   ├── embedder.py           # 嵌入：OpenAI 兼容(zhipu/openai) 或 BM25 关键词兜底
│   └── store.py              # InMemoryTaskRetriever：按 task_id 隔离的向量/关键词检索
├── api/               # 13 个 router：tasks / analytics / ingest / grading / experts /
│                      #   auth / users / courses / assignments / students …
├── state/__init__.py  # TaskStore + JobStore（OrderedDict + RLock + TTL，⚠️ 纯内存）
├── auth/              # JWT + bcrypt；seed.py 预置测试账号
├── progress/          # ProgressReporter：逐 substep 进度事件（前端可观测性）
└── prompts/           # calc.txt / concept.txt / programming.txt / proof.txt

frontend/smartai_v2/
├── pages/        (34) # 路由页：task-centric (/tasks/[id]/...) + 公开/登录页 + 学生端
├── state/        (9)  # Reflex State：task.py（核心）、auth.py、analytics.py、ui.py…
├── api/          (16) # 后端 HTTP 客户端封装（每个后端域一个模块，含 kb_api）
└── components/   (15) # 布局、图表、上传、进度、表单、auth_guard…
```

**分层落地**：一次批改请求的调用链是
`api/grading.py → grading_agent（逐学生逐题）→ classify 题型 → 对应 skill → multi_expert（1~N 专家/采样）→ 各 provider.ainvoke（带 RPM 限流）→ 工具(SymPy/沙箱/RAG) → 合成 + 公平信号 → Correction`，全程通过 `ProgressReporter` 上报 substep。

---

## 2. 各部分重点内容

### 2.1 任务状态机 + 幂等 + TaskStore
- **状态机**（[backend/models.py](backend/models.py)）：`draft → extracting_problems → problems_ready → parsing_submissions → submissions_ready → grading → graded`，任意态可进 `error`（可恢复）。
- **幂等**：每个变更端点都 `sha256(file_bytes)`，已在运行 → `already_running`；同 hash 且状态已就绪 → `already_done`；否则才启 `asyncio.create_task`。避免重复点击重跑 LLM。
- **TaskStore**（[backend/state/__init__.py](backend/state/__init__.py)）：`OrderedDict + RLock + TTL(7天) + MAX_TASKS(500)`，`list_for_owner(owner_id)` 做按用户隔离（admin 例外）。接口是 dict-like，注释明确"未来可换 Redis/Postgres"——⚠️ **目前纯内存**。

### 2.2 多专家 BYOK + 公平信号（P0 已完成）
- [backend/agents/multi_expert.py](backend/agents/multi_expert.py)：≥2 个专家时各自打分；单专家时按 `multi_sample_n` 多采样（**默认 1，省钱**，见 [config.py:81-91](backend/config.py#L81)）。
- **Indecisiveness Score (IS)**：分数的归一化标准差（std/max_score），超过 `is_threshold`（默认 **0.15**，[config.py:79](backend/config.py#L79)）→ 标记 `requires_human_review`。逻辑在 [multi_expert.py:288-340](backend/agents/multi_expert.py#L288)。
- **Minority Veto**：任一专家/采样偏离中位数超过 `minority_veto_deviation`（默认 **0.30×max_score**，[config.py:97](backend/config.py#L97)）→ 即使 IS 未超阈也标记人工复核。捕捉"两人给 9 分一人给 4 分"被平均掩盖的情况。
- **反越狱前缀**：[backend/skills/base.py:35](backend/skills/base.py#L35) `ANTI_JAILBREAK_PREFIX`，由 `build_system_prompt`（[base.py:49-56](backend/skills/base.py#L49)）统一注入，挡"忽略以上指令/给满分"类注入。
- 公平信号字段（`indecisiveness_score / needs_review / review_reasons`）存在 [models.py:56-70](backend/models.py#L56)，前端可据 `review_reasons` 的稳定字符串 ID 本地化展示。

### 2.3 四题型 Skill + SymPy/沙箱（已完成）
- **计算题**（[backend/skills/calculation.py](backend/skills/calculation.py)）4 档 fallback：① 用标答；② LLM 写 SymPy 程序跑出标答；③ `numerical.verify_equivalent/verify_value` 校验；④ 全失败回退纯 LLM。评语自动带 `（SymPy 验证：✓/✗/未启用…）`；mismatch 时分数封顶 `0.7×max`（只给过程分）。
- **编程题**（[backend/skills/programming.py](backend/skills/programming.py)）4 档：① 教师测试用例跑沙箱；② LLM 生成 ≤8 用例跑沙箱；③ 关键词命中(tkinter/socket…)或不可行 → 跳过复杂；④ 非 Python → 纯 LLM 审查。沙箱 10s 超时 + 全局 `asyncio.Semaphore(8)`（[backend/tools/sandbox_runtime.py](backend/tools/sandbox_runtime.py)，`SMARTAI_SANDBOX_CONCURRENCY` 可调）。
- **CoT 分步评分**：prompt 与 skill 里已要求逐步打分（如 [concept.py](backend/skills/concept.py) 的 `steps` 字段、[proof.py](backend/skills/proof.py) step-by-step）。
- **Rubric/criterion 注入**：4 个 skill 已接受 criterion，教师可在题目编辑里改评分标准。

### 2.4 RAG MVP（后端完成、入口割裂）
- **检索器装配**：[backend/main.py:56-58](backend/main.py#L56) 启动时把默认的 `NoOpRetriever` 换成 `InMemoryTaskRetriever`（[backend/rag/store.py](backend/rag/store.py)），按 `task_id` 隔离。
- **嵌入策略**（[backend/rag/embedder.py:223-260](backend/rag/embedder.py#L223)）：优先用 BYOK 里的 OpenAI 兼容 key（Zhipu `embedding-3` / OpenAI `text-embedding-3-small`）；都没有则退化为 **BM25 关键词检索**（`rank_bm25`，零额外 API 成本，CJK 按字符切）。→ **RAG 不强制产生嵌入费用**。
- **接入批改**：skill 通过 [backend/tools/knowledge.py](backend/tools/knowledge.py) 的 `retrieve(query, k, scope=task_id)` 取回片段；证明题 prompt 注入 `{context}`（来自教材）。
- **前端入口**：✅ 任务级知识库上传**已实装**在 Setup 页 [task_upload... / task_setup.py:147-249](frontend/smartai_v2/pages/task_setup.py#L147)（`_kb_upload_widget`，受 `config_use_kb` 开关控制），state 事件在 [task.py:1049-1118](frontend/smartai_v2/state/task.py#L1049)（`load_kb / upload_kb_file / delete_kb_doc`），走 `kb_api`（5MB 上限、同 hash 幂等、返回 chunk 数与 embedder 名）。
- **唯一的占位**：全局导航的 [frontend/smartai_v2/pages/knowledge_base.py](frontend/smartai_v2/pages/knowledge_base.py) 仍是写死的 "Coming with RAG (P2)"，其注释 "backend/rag/ is empty" **已过期失真**。
- **遗留**：内存存储重启即丢；全局 KB 页待拆；缺一个"任务级 KB 是否生效"的可视化反馈。

### 2.5 模型与限流（4 家供应商 + RPM）
- **默认值**（[backend/config.py:19-41](backend/config.py#L19)）：`default_provider="gemini"`；gemini=`gemini-3-flash-preview`、openai=`gpt-4o`、zhipu=`glm-4.5-air`（国产，OpenAI 兼容，base `open.bigmodel.cn/api/paas/v4`）、anthropic=`claude-sonnet-4-20250514`。
- **BYOK 注册**：[backend/llm/registry.py:37-64](backend/llm/registry.py#L37) `_seed_from_settings` 从 env 播种，用户也可经 `/experts` 增删。`pick_default()`（[registry.py:125-134](backend/llm/registry.py#L125)）优先选 `default_provider` 类型。
- **RPM 限流**：每 provider 一个滑窗限流器 [backend/llm/providers.py:42-134](backend/llm/providers.py#L42)（`_RPMLimiter`，`ProviderConfig.rpm` 配置；这就是最近 "add RPM waiting" 提交）。叠加 429-感知重试：`llm_rate_limit_max_retries=6` / `llm_rate_limit_max_wait=60`（[config.py:64-68](backend/config.py#L64)），尊重服务端 `Retry-After`。
- **加国产模型的接口**：因 Zhipu 已走 OpenAI 兼容路径，新增 DeepSeek/Qwen/Kimi 基本只需「config 加字段 + registry 加一段」——详见 §6.4。

### 2.6 前端动线（Reflex）
- **task-centric 路由**：所有新流程走 `/tasks/[id]/...`；旧路由是 redirect-only stub。`current_task_id` 用 `rx.LocalStorage` 持久化，刷新可恢复。
- **进度不丢**：`watch_active_job` 是绑在 State（非页面）上的 `@rx.event(background=True)` 轮询（~1.5s），切页不停——这是"切页不丢进度"的关键。
- **学情分析**：5 张 Plotly 图 + 排行榜（AnalyticsState 缓存计算）；NL 自定义查询走 `nl_query_box` 组件 + `/analytics/{id}/query`（per-owner 30s 限流 + plotly trace 白名单）。
- **认证**：真 JWT 登录 + 教师/学生 Demo Login 按钮 + `/experts` BYOK 配置 UI；`auth_guard` 按角色守卫。

### 2.7 进度可观测性
- [backend/progress/](backend/progress/) 的 `ProgressReporter` 在每个 agent/skill/tool 边界 emit substep（如 `retrieve_knowledge / llm_grade / sympy_verify`），ring buffer 200 事件/job（[config.py:100](backend/config.py#L100)），前端轮询展示"第几个学生/第几题/哪个专家/哪一步"。

### 2.8 测试
- `backend/tests/`：`test_tasks.py`、`test_calculation_skill.py`、`test_programming_skill.py`、`test_rag_inmemory.py` 等，用 mock provider，不需真 key（适合 CI / 本地快速验证）。冒烟样本在 `SmarTAI_test_case/`（`SmarTAI_hw1.txt` 题目 + `SmarTAI_test_stu_hw1.zip` 学生作答）。

---

## 3. 计划 vs 完成（对照旧文档与既往记忆）

| 计划项 | 来源 | 当时状态 | 现在真实状态 | 证据 |
|---|---|---|---|---|
| task-centric 工作流重构（Task 实体 + /tasks + /analytics） | TASK_WORKFLOW_REFACTOR_CN | 2026-04-30 完成 | ✅ 完成 | [models.py](backend/models.py)、[state/__init__.py](backend/state/__init__.py)、`/tasks/*` |
| 计算题 SymPy 4 档 + 编程题沙箱 4 档 | 记忆 sympy_sandbox | 2026-05-02 完成 | ✅ 完成 | [calculation.py](backend/skills/calculation.py)、[programming.py](backend/skills/programming.py)、[sandbox_runtime.py](backend/tools/sandbox_runtime.py) |
| Rubric/criterion 编辑链路 | AI_GRADING_RESEARCH | 2026-05-03 确认完成 | ✅ 完成 | 4 个 skill 接 criterion + 前端行内编辑 |
| P0 公平三件套（CoT + IS + Minority Veto + 反越狱） | grading_improving_plan / 记忆 | 2026-05-03 批准 | ✅ 完成 | [multi_expert.py:288-340](backend/agents/multi_expert.py#L288)、[config.py:74-97](backend/config.py#L74)、[base.py:35](backend/skills/base.py#L35) |
| JWT + bcrypt 认证 + 角色 + demo token | ROADMAP | 2026-04-26 新增 | ✅ 完成 | [backend/auth/](backend/auth/)、[config.py:106-124](backend/config.py#L106) |
| 多专家 BYOK（gemini/openai/zhipu/anthropic） | ROADMAP | ✅ | ✅ 完成（含 RPM 限流） | [registry.py](backend/llm/registry.py)、[providers.py:42](backend/llm/providers.py#L42) |
| RAG（PGVector 知识库） | ROADMAP P2 / 记忆 | 计划留"开关" | 🟡 **后端 MVP 完成 + 任务级上传 UI 在 Setup 页**；但①内存非 PGVector ②全局 KB 页仍占位 | [main.py:56-58](backend/main.py#L56)、[rag/](backend/rag/)、[task_setup.py:147](frontend/smartai_v2/pages/task_setup.py#L147) vs 占位 [knowledge_base.py](frontend/smartai_v2/pages/knowledge_base.py) |
| **OCR 多路径融合（达到 Mathpix 水平）** | AI_GRADING_RESEARCH P2 / 记忆「关键空白」 | 2026-05-03 标为关键空白、stub | 🔴 **仍未开始**（图片被丢弃，无 vision/Mathpix） | [file_processing.py:145-152](backend/tools/file_processing.py#L145)；仅人工订正端点 [tasks.py:923](backend/api/tasks.py#L923) |
| PostgreSQL 持久化 | ROADMAP P0「最关键」 | 未做 | 🔴 仍未做（纯内存） | [state/__init__.py](backend/state/__init__.py)，全库零 DB 依赖 |
| 学生端 UI | ROADMAP P1 | 部分（dashboard 占位） | 🟡 有 `student_dashboard.py` 等但优先级低 | [frontend/smartai_v2/pages/student_dashboard.py](frontend/smartai_v2/pages/student_dashboard.py) |
| 公网部署（Render 双服务） | DEPLOY_FREE | 计划 | 🟡 配置就绪、URL 占位未回填、未确认上线 | [backend/render.yaml](backend/render.yaml)、[frontend/render.yaml](frontend/render.yaml) |
| Post-Grading Review Agent（横向比对，P1） | AI_GRADING_RESEARCH | 计划 | 🔴 未做 | — |

**过期/失真陈述提醒**：
- [knowledge_base.py:24](frontend/smartai_v2/pages/knowledge_base.py#L24) 注释称 "backend/rag/ is empty" —— 已失真，`backend/rag/` 已有完整实现。
- `docs/ROADMAP.md`（2026-04-26）把 RAG 列为"未实现 P2"、OCR 未单列 —— 与现状不符，本文档取代之。

---

## 4. 当前最大技术缺口（按对「数理方向 + 公网内测」的阻塞度排序）

1. 🔴 **OCR / 手写识别（最高优先，数理命门）**——不做：学生手写/拍照的数理作业根本进不去系统，"测 OCR 准确率"无从谈起。涉及：[file_processing.py](backend/tools/file_processing.py)（加图片/扫描分支）、[backend/llm/providers.py](backend/llm/providers.py)（加 vision 调用）、ingest 流程、前端上传放开图片类型、[tasks.py:923](backend/api/tasks.py#L923) 人工订正闭环。
2. 🔴 **共享模型池的硬性额度上限（公开前必做）**——不做：把你自己的 key 暴露给测试者/申请者后，一个 50 人班级的多专家批改几分钟能烧掉数美元，恶意/手滑用户可一日清空预算。当前代码**没有**每用户额度/熔断（只有 BYOK 这一种成本隔离 + 全局 RPM）。建议加在 grading 入口或 multi_expert 之前。详见商业文档 §2。
3. 🔴 **PostgreSQL 持久化（Stage 2 必做）**——不做：进程重启/主机休眠 = 用户/任务/批改结果全丢，无法正式承接用户数据。TaskStore 接口已 dict-like，迁移点在 [backend/state/](backend/state/)。
4. 🟡 **RAG 全局页未拆 + 内存易失**——任务级已能用，但全局 `Knowledge Base` 页仍是占位；KB 重启丢失；缺"KB 是否生效"的反馈。
5. 🟡 **BYOK key 安全审查（多用户公开前必做）**——key 目前在内存，需确认：不落库、不进日志、per-user/per-request 不被下个用户继承（注意 render.yaml 警告：env 里设 key 会被所有用户继承）。
6. 🟡 **监控埋点**——缺并发用户数 / 存储增长 / 人均 token 成本的可观测指标，Stage 2 决策"何时加购"需要它。

---

## 5. 关键文件地图（新贡献者必读）

| 文件 | 作用 |
|---|---|
| [backend/main.py](backend/main.py) | 入口：代理 env 顺序、装配 RAG 检索器、挂载 router |
| [backend/config.py](backend/config.py) | 所有可调参数：模型默认、限流、JWT、公平阈值 |
| [backend/models.py](backend/models.py) | Task 状态机 + Correction + ProviderConfig 等全部数据模型 |
| [backend/agents/grading_agent.py](backend/agents/grading_agent.py) | 批改总编排（逐学生逐题、题型路由） |
| [backend/agents/multi_expert.py](backend/agents/multi_expert.py) | 多专家/多采样 + 合成 + IS/Minority Veto |
| [backend/skills/base.py](backend/skills/base.py) | Skill 基类 + 反越狱前缀 + 系统 prompt 组装 |
| [backend/skills/calculation.py](backend/skills/calculation.py) | 计算题 SymPy 4 档（数理批改核心） |
| [backend/tools/file_processing.py](backend/tools/file_processing.py) | 文件摄入（⚠️ OCR 缺口在这里补） |
| [backend/tools/sandbox_runtime.py](backend/tools/sandbox_runtime.py) | 代码沙箱 + 并发闸 |
| [backend/llm/registry.py](backend/llm/registry.py) | BYOK 专家注册（加新模型从这里入手） |
| [backend/llm/providers.py](backend/llm/providers.py) | 各 provider 实现 + RPM 限流器 |
| [backend/rag/store.py](backend/rag/store.py) / [embedder.py](backend/rag/embedder.py) | RAG 检索器与嵌入策略 |
| [backend/state/__init__.py](backend/state/__init__.py) | TaskStore/JobStore（⚠️ 内存，持久化迁移点） |
| [frontend/smartai_v2/state/task.py](frontend/smartai_v2/state/task.py) | 前端核心 State（任务、KB、轮询、分析） |
| [frontend/smartai_v2/config.py](frontend/smartai_v2/config.py) | 前端→后端 URL（改后端地址处之一） |

---

## 6. 同学1 分工：性能 / 模型 / OCR（分阶段、分点）

> **核心纠偏**：OCR 是"**先建后测**"，不是"测现有"。当前没有任何 OCR，所以第一任务是把识别链路建出来，再谈准确率。模型路线已定：**LLM-vision 优先 + Mathpix 作高精度对照/付费通道**。

### 6.1 Stage 1 前（内测可用的最小闭环）

**(A) 建 vision/OCR 摄入路径（第一优先）**
- 在 [file_processing.py](backend/tools/file_processing.py) 增加图片/扫描分支：当文件是 `.jpg/.png/.webp/.pdf(扫描)` 时，不再丢弃，而是走 vision 识别。
- 在 [backend/llm/providers.py](backend/llm/providers.py) 给 provider 增加多模态调用（Gemini / GPT-4o / Claude 三家原生支持图片输入；把图片转 base64 + 文本指令"把这页手写数理作答转成带 LaTeX 的纯文本"）。
- 识别产物接回现有的「人工订正」闭环 [tasks.py:923](backend/api/tasks.py#L923)——OCR 出错时教师/学生可手改，形成 human-in-the-loop。
- 前端上传组件放开图片/扫描件类型（题目上传 + 学生作答上传两处）。
- **Mathpix 作为高精度通道**：先不接，留好抽象接口（一个 `OCRProvider` 抽象，LLM-vision 是默认实现，Mathpix 是可选实现），等准确率测试证明确有需要再接。

**(B) OCR 准确率测试与提升**
- **数据来源**：公开手写数学/物理数据集（如 CROHME 手写公式、各类 handwritten math 数据集）+ 自采本科作业样本（找熟人同学要真实作业，注意脱敏）。
- **评测指标**：① 字符/公式级准确率；② LaTeX 结构正确率（括号/上下标/分式嵌套）；③ **端到端批改一致性**（OCR→批改后分数 vs 人工对同一份的分数）。指标③才是产品真正关心的。
- **语义统一规则**：`v_0` vs `v_o`、`l` vs `1`、希腊字母等易混项，建一份后处理规则表 + 让 LLM 在识别后做一次"上下文纠错"。
- **对标 Mathpix**：同一批样本跑 LLM-vision vs Mathpix，比准确率与单位成本，决定是否值得接 Mathpix。

**(C) 多模型流程跑通**
- 用 `SmarTAI_test_case/` 样本，对每个 provider 跑完整链路（extract → parse → grade），记录哪些模型能跑通、哪些题型翻车、报错模式。
- 产出"模型 × 题型"的可跑通矩阵。

### 6.2 Stage 1（≤10 熟人内测期间）
- 收集真实数理作业的识别 + 批改 badcase，按"识别错 / 批改错 / 流程错"归类。
- 跑通"无自带 API 用户"走共享池的体验（确认硬上限不误伤正常使用）。

### 6.3 选出能胜任本科数理批改的几个模型（评测矩阵）
- 维度：**批改准确率 / 跨学生一致性（重点，对应公平>松严）/ 单位成本 / 速度 / 是否支持 vision**。
- 候选海外头部：Claude（Sonnet/Opus）、GPT-4o/4.1、Gemini Pro/Flash。
- 候选国产头部：DeepSeek-V3/R1、Qwen-Max/通义、GLM-4.5（已接）、Kimi/Moonshot。**只接头部、跳过弱模型**（数理推理对模型能力要求高）。
- 产出：一张推荐清单 + 各模型适合的题型/价位档。

### 6.4 加国产模型（精确步骤）
因 Zhipu 已走 OpenAI 兼容路径，新增一个 OpenAI 兼容的国产模型基本三步：
1. **config 加字段**（仿 [config.py:34-37](backend/config.py#L34) 的 zhipu 写法）：
   ```python
   deepseek_api_key: Optional[str] = os.getenv("DEEPSEEK_API_KEY", "")
   deepseek_api_base: str = "https://api.deepseek.com/v1"
   deepseek_model: str = "deepseek-chat"
   ```
2. **registry 加播种段**（仿 [registry.py:52-58](backend/llm/registry.py#L52) 的 zhipu 段），在 `_seed_from_settings` 里 `if settings.deepseek_api_key: self.register(ProviderConfig(provider_type="deepseek", ...))`。
3. **providers 复用 OpenAI 兼容基类**：若该模型完全 OpenAI 兼容，给 PROVIDER map（[providers.py:256 附近](backend/llm/providers.py#L256)）加一项指向通用的 OpenAI 兼容 Provider 即可；wire 格式不同才需要写子类。
- 别忘了在 `ProviderConfig` 设合适的 `rpm`（国产免费档常 5~10/min，配合 [_RPMLimiter](backend/llm/providers.py#L42)）。
- Qwen 通义可走 dashscope 的 OpenAI 兼容端点；Kimi/Moonshot、MiniMax、DeepSeek 同理。

---

## 7. 同学2 分工：部署 / 隔离 / 存储 / 安全 / 扩展（分阶段、分点）

> 本节偏工程落地，采购与价格、平台对比见 [docs/GO_TO_MARKET_AND_OPS_CN.md](docs/GO_TO_MARKET_AND_OPS_CN.md)。

### 7.1 Stage 0（内测前必做：把它真正跑上公网）
- **回填三处占位 URL** 并保持一致：
  - [backend/render.yaml](backend/render.yaml) 的 `FRONTEND_URLS`（CORS 白名单）
  - [frontend/render.yaml](frontend/render.yaml) 的 `SMARTAI_BACKEND_URL` 和 `REFLEX_API_URL`
  - 前端→后端 URL 还有第二处：[frontend/smartai_v2/config.py](frontend/smartai_v2/config.py) 的 `BACKEND_URL`（与 env 同步改）
- **选最低档常驻付费主机**（避免免费档 15min 休眠丢内存数据 + 冷启动）。
- **Reflex 双服务**：后端一个 web service、前端一个 web service（Reflex 有 Python state server，不能静态部署到 Vercel）。
- HTTPS（多数平台自带）、健康检查（后端 `/health`、前端 `/`）。
- 设 `SMARTAI_REQUIRE_AUTH=true`、`SMARTAI_REGISTRATION_CLOSED=true`、`JWT_SECRET`（generateValue 或自设强随机，[config.py:107](backend/config.py#L107)）。

### 7.2 Stage 1（≤10 熟人）
- **保持内存存储**（按既定决策不接 DB），但**加"仅脱敏/样例数据"护栏 + 一行知情同意**（处理别人班学生的真实姓名/答案/分数属第三方个人数据，即使不落库也过内存与 LLM 日志）。
- **确认隔离已生效**：JWT + `TaskStore.list_for_owner` 已按 owner 隔离；用真实多账号验证 A 看不到 B 的任务。
- **BYOK key 安全审查**：确认 key 不写盘、不进 INFO 日志、不被下个用户继承；env 里不要放任何 provider key（render.yaml 已警告会被全体继承）。
- **共享池硬上限落点**（关键）：在 grading 入口或 [multi_expert.py](backend/agents/multi_expert.py) 调用前加每用户每日 token/次数计数 + 熔断；强制免费池单专家单采样（`multi_sample_n=1` 已是默认，[config.py:91](backend/config.py#L91)，确保不被前端调高）；与现有 [_RPMLimiter](backend/llm/providers.py#L42) 协同。具体策略见商业文档 §2。

### 7.3 Stage 2（正式推广）
- **接 PostgreSQL 持久化**：TaskStore/JobStore/用户/课程/作业全落库；迁移点 [backend/state/](backend/state/)（接口已 dict-like，封一层 repository 即可）。
- **对象存储**：学生作业原始文件 / 图片（OCR 输入）放 S3 兼容存储（R2/OSS/COS），不要塞数据库。
- **真正多租户数据分离**：DB 层按 owner_id 加约束 + 行级隔离；审计日志。
- **监控埋点**：同时在线人数、各任务 LLM 调用量、人均 token 成本、存储增长趋势——驱动"何时加购"。

### 7.4 Stage 3（规模化）
- 主机按负载升档；内存态（轮询、缓存）外移到 Redis 以支持多实例横向扩展。
- 数据库与对象存储扩容 + 定期备份 + 灾备。
- 成本随量增长的控制：BYOK 占比越高你成本越低；共享池硬上限是成本天花板。

---

## 8. 风险与注意事项

- **429 限流**：项目自身依赖的多家 LLM 都有 RPM 限制（最近 "add RPM waiting" 提交即为此）；批改是高并发场景，务必保留 [_RPMLimiter](backend/llm/providers.py#L42) + 429 重试，并在共享池上叠加每用户额度。
- **内存易失**：当前一切内存态，重启/休眠全丢——Stage 1 可接受（脱敏 + 常驻主机），Stage 2 前必须上 DB。
- **单点**：单实例 + 内存态 = 无法横向扩展；扩展前需先外移状态。
- **LLM 成本**：多专家 × 多采样 × N 学生 × M 题会成倍放大；默认 `multi_sample_n=1` 已省钱，别在共享池放开。
- **隐私合规**：处理学生个人数据需脱敏 + 知情同意 + 留存/删除策略，遵循《个人信息保护法》与校园数据规范。

---

*维护：本文档随大改动更新；配套商业/采购规划见 [docs/GO_TO_MARKET_AND_OPS_CN.md](docs/GO_TO_MARKET_AND_OPS_CN.md)。*
