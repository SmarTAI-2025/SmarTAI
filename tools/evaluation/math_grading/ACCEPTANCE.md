# 4B集成验收清单

后续复审：2026-10-21生产候选 `ccb1dd32` 的修复、完整回归及证据边界见 [本地审核报告](reports/LOCAL-REVIEW-20261021.md)；下文为原4B候选记录，不替代新候选证据。

状态：4B 本地固定响应集成验收完成，生产候选 `72377b72ff0b4b8c9539348d0eb6cdcc063da8d9`。完整结论、命令、精确版本和边界见 [最终报告](reports/FINAL-72377b72.md)。原40例/哈希保持不变；新增定向回归放在独立测试，不改低原预期。

| 门 | 输入/检查 | 必须取得的证据 | 失败归属 |
|---|---|---|---|
| 评分按标准 | 全40例，重点C01–03、C19、C24 | 分数区间命中；步骤非负且和等于总分；无匹配必满/70%上限 | 3；计划分值错交1 |
| 真实数学核验 | corpus中的16个request | 真实工具三态、条件、反例；不能拿工具状态作总分 | 2；条件提取来源错交3 |
| 同题共享计划 | C01/02/03同题；交换学生名/顺序/专家/采样 | 实际plan_id与有效标准相同；首轮在看学生前准备 | 3冻结，1计划 |
| 重试与版本 | 同输入失败重试；然后改教师标准/满分 | 重试复用实际计划；输入改变新身份；旧checkpoint不得混入 | 3 |
| 错误延续 | C08/C09；同首错多下游；两独立错 | 原文/依赖/error_id；扣一次或两次；不能仅检查不同ID | 3 |
| 账本防护 | 重复award/error，缺项，虚构quote，环依赖 | 拒绝不合法账本，有效结果保留；仅需要时一次兜底 | 3 |
| 多专家 | 同总分5/5，分项[5,0]对[0,5]；非均等权重 | 识别实质分歧；本题至多一次复核；汇总尊重权重 | 3 |
| unknown/timeout | C26/27，所有工具unknown，部分有效证据 | 有效分数保留；无新增强制处理；有限调用 | 3；错误工具状态交2 |
| 简略标准 | C28及无细则题全班 | 同一解释和来源；题目层面建议不复制给全班 | 1+3 |
| 故障与空答 | C07/K05/K06；最终保存失败 | 有效0与失败无分区分；真实调用/解析/保存错误走原恢复 | 3 |
| 整道大题 | 带(a)(b)内部结构的单q_id案例 | 一个公开max_score/result/progress；内部项目不变成题 | 3 |
| API/旧入口 | 对照基线公开schema/路由，教师与Demo调用 | 保持公开字段/含义；旧URL不可触发旧评分引擎 | 3 |
| 报告/前端 | 评分依据、建议/强制处理、恢复、下载报告 | 原有页面与报告合同测试；必要交互smoke | 3集成，4验收 |
| 收费/语言/进度 | 两语言、每题事件、provider错误 | 现有计费限流/语言/进度合同保持，不新增请求 | 3 |

上述各门的已执行证据：

- 评分、unknown/timeout、简略标准、故障空答：真实主链40例回放全部符合预期，38个有效分数、2个真实故障投影无分。`candidate-72377b72.json` 保存逐例输入、固定响应、实际计划和trace。
- 真实数学核验：16个请求通过；`verification-d6aae9ce.json` 所测工具源码在最终候选未变化。
- 同题共享、账本、错误延续、多专家、整题单元：任务4 `test_candidate.py` 定向测试，含同题改名换序、分项对立、3:1权重、同分错误请求复核、单专家幸存、原文身份、(a)(b)仍为一个结果/进度单元。中英文均检查真实提示中的语言选择。
- 重试与版本、保存错误、私有trace：最终候选 `test_math_grading_lifecycle.py`、`test_grading_checkpoints.py` 和 run/runner 回归；计划源码版本/题干/标准/满分身份另由 `test_math_rubric_plan.py` 覆盖。
- API：实际导出两版160条路由、OpenAPI与ProblemInfo，结构差异0；仅一处说明文字变化。已退役的三个URL前缀均不在路由中；旧引擎恢复隔离另有生命周期测试。
- 报告/前端：报告合同与原子生成测试通过；五个现有前端文件最终合计217项通过。首轮草稿测试因缺依赖未收集，补齐临时依赖链接后补跑11项通过，保留两份原始记录。未做在线浏览器验收。
- 收费/语言/进度：真实模型调用0；保留现有额度、语言和进度测试，并检查模型/存储错误不会变成有效0分。固定响应98次不能当成真实provider费用。

执行通过仅覆盖上述本地代码合同。真实图像OCR、真实教师分数一致性、真实模型推理质量/成本、线上部署与用户验收均未执行；不把这些门标成passed。

最初集成回归命令如下；最终候选受影响集合与补充测试的精确命令见最终报告。没有修改别人的测试来掩盖失败：

```bash
/opt/anaconda3/envs/smartai/bin/python -m pytest \
  backend/tests/math_grading_evaluation \
  backend/tests/test_calculation_skill.py \
  backend/tests/test_calculation_indefinite_integral_c.py \
  backend/tests/test_authoritative_grading_score.py \
  backend/tests/test_multi_expert_robustness.py \
  backend/tests/test_grading_run_lifecycle.py \
  backend/tests/test_grading_runner.py \
  backend/tests/test_workflow_operation_checkpoints.py \
  backend/tests/test_workflow_input_recovery.py \
  backend/tests/test_report_review_contract.py \
  backend/tests/test_atomic_report_generation.py \
  backend/tests/test_task_finalization_contract.py \
  backend/tests/test_workflow_facade_integrity.py -q
```

旧测试若仍断言“匹配满分/70%”由任务3据合同更新，任务4只报告冲突。

前端已在CI兼容Node 22.16.0环境运行以下现有测试；没有安装包或修改源码、package、锁文件：

```bash
npm --prefix frontend/app test -- \
  src/routes/tasks/FinalResultsWorkspacePage.test.tsx \
  src/routes/tasks/FinalResultsWorkspacePage.review-status.test.tsx \
  src/components/tasks/reportReviewContract.test.ts \
  src/routes/frontier/FrontierLiveDemoPage.test.tsx \
  src/routes/NewTaskPage.drafts.test.tsx
```

验收报告必须写候选精确SHA、生产文件diff、命令/结果、固定与live标签、各类别误差/误给/误扣、区间分母、依据证据覆盖率、建议/强制处理/无分率、共享规则一致性、耗时和调用量。缺少观测填unverified/null，不填写0。每个失败应有原输入、预期依据、实际分数/trace、责任任务和复跑命令。
