# OCR Harness 与知识库分层验收记录

日期：2026-09-29。工程证据与模型质量证据分开；本文件不构成上线或教师验收。

## 1. 当前结论

- A-D 的共享识别、E/F/G 的题目/教师资料/学生作答、H 的完整知识入库、I 的版本化检索已经分别提交为依赖 PR。J 收口修复和评估工具单独一个 PR；禁止合并。
- 本轮没有新增 OCR 厂商、下载模型、订阅或共享 Key。题目/作答继续忠实识别；知识库使用独立经济策略，但复用源文件、可终止 PDF 工具、engine、预算和证据服务。
- 真实 AA/手写准确率、教师修改量、同模型 E0/E1/E2 消融和百度 E3/E4 对照仍为 **unverified**。没有用 fake 测试代替这些结果。
- 计算题/编程题新增教材片段注入尚未实施，等待用户确认向其当前 BYOK 模型发送已选教材片段的范围。现有概念/客观/证明题检索路径保留，增加可追溯引用。不能宣称所有题型都已接入 RAG。
- 索引当前是本地 BM25 加有限术语桥接，适合题号、术语和公式片段；不是语义向量检索。第8节补齐了改写/中英文术语/无答案的本地诊断及旧索引版本兼容，但真实教材、任意跨语言和缺少术语的查询仍没有代表性留出集证据。

## 2. 验证证据

| 范围 | 结果 | 边界 |
|---|---|---|
| 整合后端首次全量 | 3337 passed / 30 skipped / 13 failed，483.10 秒 | 记录原始失败，不改写成全绿 |
| 上述失败分类 | 预检 artifact fence、原件 owner 二次校验、前端错误代码缺失为实际回归；其余为旧 mock/输入合同 | J 修复并定向复测 |
| J 第一组合定向 | 137 passed / 1 failed；失败是新增测试导入位置错误 | 导入修正后纳入下一组 |
| J 最终后端组合 | 121 passed，28.50 秒 | acceptance、knowledge ingestion/retrieval、executor、fusion；含新增缺口修复和全部 acceptance 测试 |
| 上传格式最终核对 | 30 passed，12.68 秒 | ingestion/personal knowledge；四种未公开格式拒绝，PDF/文本及历史行为回归 |
| PostgreSQL 收口 | 本地隔离 PostgreSQL 16.15：23 passed，9.00 秒；head/base/head 迁移往返通过 | CI36479764475发现旧并发夹具未创建可读教材块；补齐两处夹具，不放宽未就绪教材门禁 |
| 非识别动作零 OCR 防回归 | 原有生成、批改/重批、编程测试执行、分析、模型/百度凭据、原件/引用读取、material apply 测试加 dispatch 禁止计数；组合196 passed | `backend/tests/conftest.py`列明受保护模块/动作，直接拦住共享runtime、旧skill和provider边界；不重复建立另一套同义测试 |
| 资料挂载/进度/apply 补充 | 38 passed / 32 deselected，4.91 秒 | course library、task history、受影响 atomic apply；同一零 OCR guard，不重跑全量 |
| 前端全量 | 86 files / 409 tests passed | J 最后状态刷新前；不称最终 head 全量 |
| J 前端定向 | 错误文案相关 16 passed；最终知识状态/检索/引用 7 passed | 最后新增 active retry 轮询测试通过 |
| 前端构建 | build 通过；只有既有大 chunk 提示 | 最后状态字段变动另行 typecheck |
| 单书实际入库 | 合成 1000 页 PDF，42 批，1000 chunks，零 provider calls；首批 5.858 秒，总 243.140 秒 | 本地机器、native 文本，不是扫描书 OCR；修复 economy-math guard 前的完整运行，后用定向测试证明 guard 一致性，未重复压力测试 |
| 单书派生证据 | 3,896,970 bytes；coverage complete，complete_with_warning | 不包含原 PDF/数据库索引/副本；数学和总体精度未校准，警告不是准确率 |
| 五书容量 | 1000+500+500+250+250 页；2,981,961 字符，15 个首/中/末精确题号，Recall@5=1.0 | 合成、精确题号，不是自然语言/真实教材留出集 |
| 五书成本/时延 | index 10,079,651 bytes；首次 0.8285 秒；热 p50 0.0037 秒、p95 0.0046 秒；build=1；provider=0 | 单机局部测试，非部署并发 SLA，不包含 OCR 入库 |
| 批改规模 | 40x10、100x10 作答单元，保留原始错误文本与反序 q_id；一个故意失败仅影响该单元 | 实际 batch orchestration + fake expert，不是模型批改能力 |
| 浏览器实际链路 | 合成 110 页 PDF 上传、显式补缺重试、尾页检索、页 110 原件预览 | 未配置模型，零真实 provider；native 内容可检索，图像缺口保持 partial |
| 移动预览 | 320px 视口 scrollWidth=320，PDF 238x337、2485 个非白/深色像素；390/1280 截图 | 截图为合成材料，无学生数据；不等于所有入口人工验收 |

完整后端最后 head 的 CI 独立记录在 PR/项目导航中；本地没有为每个文案或测试夹具变更重跑全套。

## 3. 修复与用户体验

1. 同步材料预检处于 `preparing` 时还没有 worker lease。现在只允许未过期、同 owner/task/attempt、无 lease 的该状态保存 artifact，并在事务内写入 fence；pending/running/expired 不能借此绕过。
2. 缓存原件同时核对实际 stored-file id 和 owner。学生 revision 只显式授权该学生及任务教师，不跨用户复用相同 SHA。
3. 无视觉模型时，知识库可保留带图 PDF 页的 native 文本和原始证据。该页依然 failed/incomplete，并计入 `partially_searchable_pages`；有文字不意味着图表已读完。题目/作答策略不因此放宽。
4. `knowledge-economy-v1` 的干净数学 native 规则在 planner 与 executor 中一致；保留 `knowledge_native_math_unverified`，不误报 plan changed、不增加调用。
5. 缺页重试后状态每 5 秒只读刷新，终止后停止；不由刷新发起 OCR。用户不用反复刷新整页。引用保留版本、页码、hash、artifact 和缺口警告。
6. 47 个新识别错误代码有明确中文/英文动作，提交不明不引导盲目重试；未知识别不写成学生答错。
7. 公开知识库上传保持已确认的 PDF/TXT/Markdown 范围，其他格式先转 PDF。既有 Office/native 读取与历史文件下载不删除，内部原语能力不等于新增公开格式承诺。

## 4. 原 05/06 追踪

原规格与完整逐条迁移表保留在 canonical 导航 `2026-09-28-ocr-research/IMPLEMENTATION_HANDOFF_V2.md` 的第 8、9 节。下面是其实现证据索引；“工程已接”不替代真实质量验收。

| 原条目 | 当前实现/验证入口 | 状态 |
|---|---|---|
| 05 §3.2、§4.1.1/2、§5.1/5.2.1-8 | `backend/agents/recognition_agent.py`、`backend/recognition/{planner,executor,locator}.py`、`backend/skills/recognition_reader.py`；对应 planner/agent/reader/executor/locator tests | 工程已接；模型定位/准确率待测 |
| 05 §4.1.3/4、§4.9/10、§6.1/6.2.1-10 | `backend/recognition/{fusion,quality,recheck,repair_response,repair_selection}.py` 与 recheck 系列 tests | 忠实候选/有界 patch；语义正确性不能由规则证明 |
| 05 §4.1.5、§4.3、§4.6/7、§5.2.10/11 | models/workflow_v2/artifact_codec、PDF evidence 工具；evidence、codec、question_score_contract tests | 工程已接；大题唯一计分、小问只作证据 |
| 05 §4.5/8/11、§5.2.9、§6.3/4 | budget/runtime/durable_records、recognition_runs/calls/results/artifacts；reuse/durable/cache tests | 一次额外预算、双账本、scope/cache/不明提交不重放 |
| 05 §1/2、§4.2/4、§5.3/4、§7/8 | 六用途薄 adapter、分阶段 PR、既有最终复核；无新厂商/中间复核/解题 | 工程边界保留；教师体验待真实验收 |
| 05 §9.1.1-16 | test_recognition_*、test_question_score_contract、test_provider_recognition_limits、test_pdf_* | 合同与 fake 回归；不等于真实字符/公式正确率 |
| 05 §9.2/3、§10 | 本目录离线评估器、已有 OmniDocBench adapter、下面消融协议 | 工具已交，真实样本/预算/GT 尚缺 |
| 06 §2.2/3.3/3.4/4.1/4.2/5 | `services/question_sources.py`、problem_extraction/assignments、task_preparation、question_preparation_agent；test_question_recognition_adapter | 题目入口工程已接 |
| 06 §6.1-4 | task_preparation material preflight/plan/apply；test_material_recognition_integration | reference 来源分离；rubric/tests opt-in；candidate 不能自动执行 |
| 06 §7.1/2 | task_facade、submission_source_pipeline、submission_uploads；submission source/outcome/recovery tests | 逐 source、原错/顺序/身份、只补失败；旧上传委托同服务 |
| 06 §8.1/8.2.1-8/8.3 | knowledge/{service,ingestion,native_worker,evidence}、knowledge repositories、migration 0018、三上传入口 | 全页批处理、部分可检索、恢复/删除 fence；64 MiB/1000 页与派生限额 |
| 06 §9.1/2 | 下方活入口/零 OCR 表及第8节补充 | 挂载路由、共用adapter和零调用动态守卫组合验证；不是所有参数穷举 |
| 06 §9.3/10.1-3 | RecognitionEvidenceSummary、KnowledgeIngestionStatus、KnowledgeSearchPanel、KnowledgeCitationPreview、PdfPreview、ReviewDetailPage、submissionSourceOutcomes | 前端回归 + 110 页真实浏览器工程验证 |
| 06 §1/2.1/2.4/3.1/3.2/4/11 | V2 责任/顺序/共用基础、原 07 身份映射合同、已确认架构 | 不改合作方所有权/计分/原件保留，旧文档不覆盖 |
| 06 §12.1/12.2 | 共用 recognition tests + 六用途 integration、知识版本/权限测试 | fake/本地合同已验证；真实用途质量待测 |
| 06 §12.3/12.4/13 | 本记录及 PR exact-head 证据 | 真实效果、独立当前 head review、用户验收仍未完成 |

### 活入口与零调用边界

| 路径/动作 | 当前证据 | 边界 |
|---|---|---|
| task extract、question preflight、assignment import-file | 都由 question_sources 委托 RecognitionRunService；text bypass 和来源/fence 定向测试 | 只有识别动作可调用 engine |
| reference/rubric/tests，包括两个快捷 upload | 同 preflight/plan；快捷入口只建立 review plan；unsafe apply 拒绝测试 | 不自动应用或运行测试 |
| task parse/retry、student/teacher upload | task adapter / submission_uploads；owner 映射、逐 source 保留 | 未挂载的 legacy helper 不代表线上入口；未删除历史兼容代码 |
| course-materials、personal knowledge、task KB | 同 `knowledge.service.ingest_document` | 选择已入库材料只关联，不重新入库 |
| RAG search / citation / citation download | `test_search_citation_api_is_owner_scoped_and_read_only` 将 ingestion 设为 forbidden，provider_calls=0 | owner、删除/原件失效、版本均测试 |
| Preview/material source content、状态/列表、原件下载 | API 读取持久记录/artifact；预检 preview/owner 测试，浏览器状态 GET | 不由 GET 恢复任务 |
| 生成参考答案/rubric/solution_code、mapping/apply、开始/重批、测试运行、analytics、凭据验证、导出 | 现有动作测试加入多层dispatch禁止计数；原组合196通过。第8节增加导出HTTP及finalization/workflow/KB关联等动态保护 | 动态覆盖以conftest显式清单为准，不声称每种参数/别名都单独端到端验收；导出不再仅有静态证据 |

## 5. 真实消融执行协议

保留源 SHA、用途、明确页/题范围和去标识样本 ID；diagnostic 与 holdout 分开，GT 必须对照原图复核。使用用户指定的现有账号/route 与费用或调用上限。不要在报告、日志或 Git 保存 Key/学生正文。

| Arm | 含义 |
|---|---|
| E0 | 冻结现有同一 LLM 的原链路输出，记录 native 跳过行为 |
| E1 | 同 LLM、同样本的新 harness，不允许额外复读 |
| E2 | 复用 E1 初始候选，只允许规则触发的一次补读；同时记修好/修坏/未解决 |
| E3 | 已接百度原始识别，无隐藏 LLM |
| E4 | 复用 E3 初始候选做可用规则处理；不强迫 OCR-only 语义 patch |

离线命令：`python -m tools.ocr_benchmark.evaluate_harness /absolute/path/private-manifest.json`。
该命令只读保存结果，不执行任何 provider。输入最多 16 MiB / 1000 samples。每个 sample 包含 `id, source_sha256, purpose, split, ground_truth, ground_truth_reviewed, critical_literals, outputs`；purpose 为 problems/submissions/reference/rubric/test_cases/knowledge。每个 arm 包含 `text, source_sha256, route_fingerprint, model, initial_candidate_sha256, calls, input_tokens, output_tokens, duration_ms`。未知用量填 null；不可用 arm 省略。样本 ID 也必须去标识。

配对 arm 校验同 source/route/model；E2/E4 必须指向同初始候选 SHA。结果仅包含 hash/计数和汇总，不输出正文/GT。`alignment_error_ratio` 是标准库序列对齐差异，不冒称标准 CER、公式等价或 OmniDocBench 官方分数。缺 GT/arm 保持 unverified。关键字面量指标需要人工标注公式、负号、条件、原始错误与涂改；图表和整题语义仍要原图复核。

最终验收还需要：真实题号完整率/跨页漏项，原错保留率，整题失真率，教师修改次数/用时；教材真实查询 Recall@k/引用页正确率/是否支持结论、无答案查询；首次入库、首批可用、后续检索与批改等待分开记录。依据这些结果再校准策略，不先声称全局最优。

## 6. 成本与待办

- native 文本优先，热检索不调用 OCR/LLM；只有业务已有批改路径才发送少量当前选中的检索片段。不要将 2500 页每次塞入提示词。
- 上传原件最多 64 MiB/书；单书 1000 页；持久派生 evidence 上限 128 MiB/书，所有版本累计 chunk 文本上限 32 Mi 字符。索引约束 100k chunks / 16 Mi 字符，缓存约 96 MiB / 16 entries。达到上限明确报错，不能悄悄截尾。
- 这些限额不是 2500 页任意高密度教材必定可容纳的保证。扫描原件、图片密度、公式多少、旧版本保留都会影响存储；原件配额、派生证据与进程缓存分别计量。当前没有自动跨用户去重或收费实现。
- knowledge 每页初读至多一次；额外调用每页至多一次、24 页批次至多 2 次、全书至多约 2%。不明付费提交永不自动重提。题目/作答不复用知识低成本最终结果充当高保真结果。
- 真实 OCR 对照的具体账号/模型、样本可发送范围、调用/费用上限待确认；环境中没有本轮指定的测试凭据，不扫描或导出用户 BYOK 秘钥。
- 教师复核与跨语言/语义留出集未完成；外部 embeddings/reranker/OCR 新服务不在本轮实现。不能把 J 工程 PR 标作“05/06 全部质量验收完成”。

## 7. 教材后台进度可见性补充（2026-09-29）

- 工作台增加独立教材识别列表，进行中优先，显示最近5本并链接全部记录；原统计卡明确为进行中作业。历史任务增加作业批改/教材识别页签，教材名称搜索、状态筛选和分页不会改变批改筛选或触发模型。
- `GET /knowledge/activity`只读持久摘要，涵盖同owner的个人/课程及任务专用教材；分页最大100。当前job状态优先于旧可检索版本的ready状态；原件不可用/待删除与他人资料不出现。SQL分页，不读取书页正文、不创建job、不调用OCR。
- 显示教材名、文件名、更新时间、已处理页数/比例、可检索/失败/待核对/空白数量。100%指处理过全部页，不代表成功；partial/failed/有警告用警示色，未知总页数不编造百分比或ETA。新识别未完成而旧版本可用时分别说明。每本只展示当前/最近识别状态，不新增不可删除的永久操作审计历史。
- 活跃时每5秒集中读取一份列表，终止后停止定时刷新；切回窗口可刷新。详细页与原暂停/继续/显式补缺按钮复用现有组件，只有用户点击动作才发POST，行组件不另启动重复轮询。
- 后端初次影响面36通过/1新测试夹具错误（直接构造cleanup状态违反数据库约束），改用正式删除入口后新增组11通过；既有26项未重复跑。前端3文件18通过，最终进度条颜色/可访问性调整仅组件7通过；typecheck、build、visible-scope审计通过。最后统计标签为文案变化，未再全套构建。重叠数量不相加。
- 浏览器合成接口检查工作台/教材历史、1280与320布局；320时scrollWidth=320，长文件名换行，最终工作台0 console errors。截图在`frontend/app/output/playwright/kb-progress-*.png`，未提交。浏览器夹具早期URL解析与任务响应形状错误已修正，不是产品失败；不把合成界面验证称真实provider或部署验收。
- 这是进度可发现性补充，不关闭第1/5/6节的真实OCR质量、语义检索、其他题型接线及独立review门槛。未新增供应商/订阅/通知服务，未merge或部署。

## 8. 未阻塞工程收口（2026-09-29）

本节对应用户“先补齐所有可以独立完成的工作”。基于PR111，单一增量PR；不用subagent。没有新增教材外发、厂商、模型下载或付费调用。

### 8.1 已补齐的工作

- 挂载入口矩阵：`test_ocr_mounted_routes.py` 的21项HTTP合同覆盖两个题目预检别名的四种用途、三种资料preflight、两个辅助upload、assignment import、task extract/parse、学生/教师单份上传和三个知识库上传入口。测试在共用reader/queue/ingest边界设探针；真实共用实现另由现有adapter、worker、恢复测试验证，不冒称21项都跑过真实模型端到端。
- 零OCR矩阵：`conftest.py`复用现有生成、开始/重批、runner、分析、凭据、原件/引用读取、apply测试的禁止dispatch计数；本轮新增finalization、导出生成/下载/重复刷新、workflow、KB关联、活动列表模块。`test_ocr_readonly_routes.py`实际走HTTP生成并下载ZIP，零OCR。
- 零runner矩阵：资料识别、解析、候选生成/映射/apply阶段对sandbox/subprocess/函数执行/统一runner入口设动态禁止计数。未确认、parser失败、低把握、映射不明都有对应测试；既有显式编程批改runner测试不被禁用。
- 检索纠错：未知题号不再退化为其他题号的普通词命中；纯公式查询区分正负指数；普通正文的9.8不当题号；抑制无意义提问词。增加27组可审计的常见中英文数学/计算机术语，只扩展本地索引，不改原文、不调用LLM。邻接补块也遵守每页两块预算。
- 冻结兼容：新快照使用`bm25-cjk2-terms-v2`；旧`bm25-cjk2-ids-v1`批改快照仍保留旧分词/排序规则与缓存隔离，未知或混合索引版本明确拒绝。没有重跑OCR或更换教材内容版本。
- 引用可信度：现有RAG提示明确“检索命中不等于支持结论”、冲突来源分开、参考文字不能成为指令。低把握/未校准/覆盖不完整的引用在原文按钮旁可见；长文件名在320px换行。课程资料接口声明的公开格式收敛为PDF/TXT/MD/Markdown，与实际校验一致。

### 8.2 检索诊断与成本

`retrieval_cases.json`为自行编写的23页短文、32个查询，不是实际教材留出集。`evaluate_retrieval.py`执行真实本地索引代码，`RETRIEVAL_DIAGNOSTIC_RESULTS.json`保存逐项页号结果；不包含私有教材或学生数据。检索页命中不能自动验证批改结论是否得到支持。

| 诊断类别 | 旧版 | 新版 | 边界 |
|---|---|---|---|
| 中英文术语6项 | Recall@5=0 | Recall@5=1 | 仅词表覆盖范围，不是通用跨语言能力 |
| 无答案7项 | 正确留空5/7 | 正确留空7/7 | 未证明所有无答案问题都能识别 |
| 改写6项、术语6项、题号2项、公式2项 | Recall@5=1 | Recall@5=1 | 不用已通过的合成例子夸大提升 |
| 冲突多书1项、单书限定1项 | Recall@5=1 | Recall@5=1 | 两本不同约定分别保留，不合成为一个“真答案” |
| 未支持法语1项 | Recall@5=0 | Recall@5=0 | 显式记录失败，不改标注或补齐该例掩盖限制 |

两版均零provider调用；保存的毫秒级小样本时延只说明本机诊断开销，不是生产SLA。另复用了2500页/五书真实本地索引容量回归，新版仍通过精确页号、缓存只构建一次及热p95<2秒断言。没有重跑耗时的1000页入库压力测试，因为本次未改入库和PDF/OCR worker。

复现：`python -m tools.ocr_benchmark.evaluate_retrieval`；基线追加`--index-version bm25-cjk2-ids-v1`。允许显式传入自有JSON语料（最多16MiB/10000页条目/1000查询）；输出只有页标识与指标，但自有书/查询ID也应去标识后才分享。

### 8.3 验证记录

- 首组检索16通过/1容量用例未选；后续入口/导出组合52通过/8新测试夹具失败。失败原因是TXT按设计绕过视觉adapter，改成图片夹具后入口21通过。
- 较大影响面组合138通过/1新断言字段错误；修正后，最终检索/快照/资料安全组合32通过（包含新增旧版兼容、2500页测试）。数量重叠，不相加成全量；旧失败保留，不改写为首轮全绿。
- 前端两组件5通过、typecheck/build通过。最后去重提示及长文件名样式通过浏览器检查，不为样式重复全套构建。已有大chunk构建警告仍在。
- 合成浏览器检查1280/320的引用提示及检索显示。初次预览环境变量拼错导致本地8000连接失败，修正为`VITE_SMARTAI_BACKEND_URL`后合成接口正常；没有连接真实模型。截图在`frontend/app/output/playwright/ocr-completion-citation-*`，不提交。
- 主线程完成diff自查；没有独立review、最终head全套CI、merge、部署或真实教师验收证据。此前J整体CI失败及后续本地PG修复记录不因本PR被改写。

### 8.4 必须保留的未完成项

1. **真实OCR质量验收**：需要指定现有可用BYOK/百度路由、允许发送的样本范围和调用/费用上限。AA七题、真实手写的原错/删改/跨页和教师修改用时仍未验证。代码工具与本地合同不是模型准确率证明。
2. **真实检索/引用验收**：需要代表性教材和教师认可的查询/参考页/支持关系，诊断与留出分开。此类样本可以先仅在本地做检索，不必发送外部模型；任意同义/跨语言召回不能靠小词表承诺。
3. **计算/编程题的新增教材上下文**：此前外发授权问题尚无回复，未加入其LLM prompt；现有概念/客观/证明题路径不变。明确确认仅向本次选定BYOK发送任务主动选中教材的少量片段后，才能补该接线与测试。
4. **独立当前head代码审查及正式上线验收**：按用户指令不使用subagent，不以主线程自查冒充独立review；PR保持未合并。公开发布仍受项目安全发布门禁约束。

结论：本节关闭已知的未阻塞入口/零调用验证和本地检索诊断缺口，不把整个05/06或真实OCR效果标为全部验收完成。
