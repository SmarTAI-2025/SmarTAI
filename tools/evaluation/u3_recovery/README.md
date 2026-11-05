# U3：旧材料关联与恢复核查（Mac，2026-10-28）

本次核查在 Mac 本地执行，基线为 main `101352c8a7df4e628d2716a2731b702d5f294a8d`，源码树 `e2e80a85e87548e044042a791b713eafc19ed209`。GitHub 当前分支读取与本地 tree 已交叉核对。这里只新增核查工具、合成样本和报告，没有修改产品、前端或同学负责的实现。

结果：旧跨页/恢复反例在本次离线复测中不再出现；另确认 **2 项仍存在的输入/题目映射缺口**，均交文档 07 的实现负责人处理。没有确认独立于该链路的新通用 OCR／跨材料恢复候选任务。

2026-10-28 主审核补测：修正 M2 验收脚本只搜索任意 JSON 原文的漏检，新增身份/原字段/未决来源断言、17 个错误投影变异、唯一完整题号正控，以及实际在途取消/晚到/教师编辑组合。普通pytest发现不再被U3专用入口中止。当前独立集合为 **34 通过、2 原始失败**；其中产品行为11通过/2失败，断言自测19通过，发现/安全入口4通过，后两类不计为产品通过。相关回归221通过。两项产品缺口仍未修复。范围对应见 [CHECKLIST.md](CHECKLIST.md)，补测细节见 [FOLLOWUP.md](FOLLOWUP.md)。

## 证据边界

- 全部样本为构造数据；不含生产学生数据。模型/OCR返回固定，实际调用当前服务、SQLite、文件存储、artifact 和流程门禁。固定响应调用计数不是供应商账单，也不证明视觉 OCR 准确率。
- Python 3.12.14，锁文件适用的 100 个包版本全部匹配；macOS 15.6.1 arm64。前端 Node 22.22.2、npm 10.9.7，依赖锁 SHA256 `7ae4c47ef046edf34986bd07bd7d7ad7bb9e6e85223d43d5e49cb74e3c900dea`。
- 每次运行清空继承环境，拒绝源码目录 `.env`，创建独立 SQLite/本地存储；默认阻止 IP 连接和外部 DNS。全量后端的真实本地 HTTP 测试显式开放字面回环 IP，外部连接仍禁止。
- 进程恢复用独立 Python 进程 `os._exit(86)` 制造硬退出，再启动新进程。租约到期通过测试数据库加速；不是实际断电、Mac 睡眠或自然等待租约实验。
- 审计已知缺口保留原始失败断言，**没有 xfail、产品修复或“全绿”包装**。此目录显式运行，不自动加入 `backend/tests` 的产品回归集合。

## 本次已修边界／未再复现的历史反例

| 历史形状 | 本次核验 | 当前结论及限制 |
| --- | --- | --- |
| PR257 同题两页标准/标答被截断，后段被当冲突 | `test_cross_page_teacher_sources.py`；新增三页按 3→1→2 到达 | 原文字段完整，最终按 1→2→3，每段恰一次；缺中页保留未决证据 |
| PR260 指定目标遗漏、400字重叠重复抽取、同号不同题被去重 | `test_targeted_question_context.py` | 漏目标仍未完成；重叠不增加相同题；不同题干同号并存。公共定义另见 M1 |
| PR261 乱序/短号/跨页作答误配、格式修复抹掉模型归属疑点 | `test_submission_question_matching.py`、`test_submission_partial_responses.py` | 不确定归属继续保留；汇总前中断按每题所采用版本恢复审核来源 |
| PR274 7题成功、材料失败后重试变14题 | `test_question_retry_identity.py`；新增 `[1,7,1]` 连续两次失败重试 | 成功来源不重抽、不重复生成；人工接手零新模型调用 |
| 已存字段但汇总未完成时中断；丢失/篡改恢复artifact | `test_question_preparation_recovery.py`、OCR artifact/cache回归 | 正常保存成果可找回；输入/归属被改或证据损坏时拒绝复用 |
| 处理中暂停、晚到结果覆盖人工内容 | 既有增量材料、停止/恢复和持久租约回归；新增3路实际pipeline暂停/取消/编辑组合 | 首块已存、第二块在途、第三块排队时停止；第三块零调用；晚到第二块不能落盘/覆盖教师编辑；原来源歧义按同一issue完整保留。旧进度计数测试仅证明计数，不单独当内容证据 |
| 成功页落盘后进程退出；第二页已提交但结果未知时退出 | 新增 `test_process_recovery.py` 两路独立进程 | 成功页不重复处理；未知提交恢复为 `provider_submit_uncertain`，刷新不重放，显式确认后只重试所需页 |
| PR276 旧错误带入全新执行 | `test_recognition_stop_resume.py`、`test_submission_parsing_recovery.py`、timeout/lease回归 | 配置重启全新执行，失败项重试保留成功；两种语义分别核验 |

可选标答不完整投影为 warning 符合当前合同；题干/评分标准的实质缺失仍阻断。不能沿用“所有缺页一律阻断”旧预期。

## 仍可复现 M1：跨块请求缺少前置公共定义

这是 PR260 明确暂缓的历史反例，本次重新构造后仍成立。

1. 读取 [long_shared_definition.txt](samples/long_shared_definition.txt)，共 27,756 字符，选择题号 1、2。
2. 源文在第1题前定义 `a star b = a + 2*b`；第2题在长段落之后引用同一定义。
3. 使用当前真实 24,000 字符分块、400 字符重叠；不缩小阈值，不替换分块器。
4. 两次抽取请求长度为 24,000 和 4,156 字符。第2题请求的正文和 `source_context` 都没有公共定义；上下文只有 `Section 4`。

对应 `probes/test_mapping.py::test_long_selected_source_retains_shared_definition_in_later_request`。固定合法抽取响应可保存该题，且没有针对上下文缺失的自动问题。结论是**请求丢失上下文**；没有测试真实模型会不会据此生成错误答案。原始来源文件仍保存。

当前责任位置：`backend/agents/incremental_question_preparation.py:695-717`。归属 **07 输入／题目上下文映射**，同学实现，U3 用同一份样本复测；不另开通用 OCR 修复。

## 仍可复现 M2：重复题号的未归属标准未进入人工投影

这是本次新增的合成反例，不代表已经发生过生产事故。

1. 输入 [duplicate_number_problems.txt](samples/duplicate_number_problems.txt)：不同矩阵题都编号 7，分别带已有评分标准和标答。
2. 再输入 [duplicate_number_rubric.txt](samples/duplicate_number_rubric.txt)：仅标“Question 7”的独立标准，无法确定归属哪一道题。
3. 当前实现保留两道题，也把独立标准原文存入不可变 material unit。
4. 停止后走原人工接手 API：两题均只显示原标准；新增标准及其未解决归属问题未进入最终/人工矩阵，`preparation_issues` 为空。
5. 无学生时 readiness 只有 `submissions_required`；实际下一阶段上传排队返回 `started`。测试没有执行该队列或真实评分。

对应 `probes/test_mapping.py::test_ambiguous_number_material_remains_visible_as_unresolved_after_manual_handoff`，证据见 [duplicate_number_material.json](evidence/observations/duplicate_number_material.json)。

补强后的同一反例首先检查保存及人工投影中两题的存储身份、题号、题干、原标准/标答与来源未被错配，再检查未归属原文、source token 和开放歧义问题在现有复核投影可见。任意 JSON 包含原文不再算通过；原文偷偷替换/追加到任一题、伪造身份/来源、重复材料和静默解决均由变异测试抓住。正控使用明确的 `1.7` 与 `2.7`，仅完整编号 `2.7` 的题接收该来源。

原上传和不可变材料仍在；缺口是**人工处理投影和未决提示遗漏**，不是原始文件物理删除。也不能据此声称真实评分已经错误放行。用例要求保留原文与未决归属，不要求静默猜配、添加某个新控件，或把所有材料歧义改成 blocking。

当前责任位置：`backend/agents/incremental_question_preparation.py:331-364`。归属 **07 输入／材料与题目映射**，同学决定现有流程内的保留与提示方式，U3 原样复测；不接管实现。

## 执行结果

- 既有相关回归：原始 474 次断言用例执行，40 个重复覆盖；**434 个不同用例通过**，只按去重数量报告。
- 初次独立审计：**7 通过、2 预期行为断言失败**。主审核后扩为产品行为 **11 通过、2 失败**，另有 **19 项断言自测、4项发现/入口校验通过**；合计34通过/2失败，相关回归另221通过。两项失败仍是 M1/M2，没有修复、xfail 或失败过滤。
- 前端：**1,556 通过**；类型检查（含 E2E 类型）、范围审计、public build 通过。634 个前端受版本控制文件与 main 完全一致。未运行浏览器。
- 后端全量：**7,464 通过、101 条件跳过，0 失败/错误**；已经包含434项定向测试，不再相加。完整分层结果见 [VALIDATION.md](VALIDATION.md)。
- 审计入口拒绝直接启动 worker，以及拒绝带 `.env` 源码目录的校验通过；相关代码经过独立只读审阅，修正只涉及本审计工具。

原始测试分层与环境校准均保留：旧 `smartai` 环境缺依赖、最初源码归档无 `.git`、沙箱不允许本地 HTTP 绑定属于测试环境问题；均不计为产品缺陷。进程样例初版取错 DTO 路径/默认替换型 stub 的校准也不计产品缺陷。

## 最小重跑命令

从没有 `.env` 的当前仓库根目录，用匹配 `requirements-dev.txt` 的 Python 执行。样本 SHA256 见 [manifest.json](samples/manifest.json)。输出中的 `runtime` 包含本次独立数据库、观察数据和 `run.json`；请勿提交这些运行目录中的完整临时数据库。

```bash
# 全部独立样例：当前main应为34 passed、2 failed，退出码1。
# 其中19项为断言自测、4项为发现/入口校验，不是产品行为通过。
python tools/evaluation/u3_recovery/run_offline.py -p backend.tests.conftest -q -s tools/evaluation/u3_recovery/probes

# 只重跑两个已知反例：当前main应为2 failed，不能当修复验收。
python tools/evaluation/u3_recovery/run_offline.py -p backend.tests.conftest -q tools/evaluation/u3_recovery/probes/test_mapping.py -k 'long_selected or ambiguous_number'

# 已通过的独立边界、断言与入口自测：应为34 passed。
python tools/evaluation/u3_recovery/run_offline.py -p backend.tests.conftest -q tools/evaluation/u3_recovery/probes -k 'not long_selected and not ambiguous_number'

# 既有后端全量；部分真实本地HTTP回归需要回环连接与本机监听权限。
python tools/evaluation/u3_recovery/run_offline.py --allow-loopback -q backend/tests
```

`--source-root /path/to/clean-checkout` 可将核查脚本用于另一个精确 checkout；部分既有测试需要 `.git` 元数据。不要直接执行 probe 或 worker，入口会拒绝。

普通pytest目录发现自动忽略本目录的专用probe（单独扫描本目录时0项、pytest退出码5；混合普通测试时不影响其收集）。显式指定probe文件或直接导入仍在backend导入前拒绝。只有隔离启动器会正常收集全部36项；未修改仓库根pytest配置或CI。

## 未测与交接

真实视觉 OCR/手写准确率、真实模型抽取/收费量、浏览器点击暂停/刷新/重进、PostgreSQL、对象存储、Mac 休眠/断电/磁盘满、生产历史数据迁移、真实 OCI 与编程评分质量均未作为本轮验收。编程用例恢复、编辑即执行及评分状态由 U2/同学链路覆盖，不据本报告宣称通过。

M1/M2 交07实现负责人按当前架构处理；U3复测时必须沿用本样本、记录新准确SHA，并分别确认模型输入上下文、未决材料的可见保留。U2可引用本报告的精确版本，避免重复开发。若后续出现独立通用 OCR／跨材料恢复反例，先列候选再决定实现归属。
