# 4B集成验收清单

状态：全部 `not_executed`。触发条件：任务3给出 integration_ready、准确SHA、工作区和执行入口。保持同一40例/哈希；新增反例并列版本，禁止为候选改低原预期。

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

4A只有技能级回放和旧多专家review-signal探针，不把本表状态改成passed。真实图像OCR、真实教师分数一致性、本轮收费模型评测均未执行。

候选到位后按实际变更选择现有回归，不修改别人的测试来掩盖失败：

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

前端在CI兼容Node环境、依赖已就绪时运行以下现有测试；本轮没有运行，也没有安装/修改前端：

```bash
npm --prefix frontend/app test -- \
  src/routes/tasks/FinalResultsWorkspacePage.test.tsx \
  src/routes/tasks/FinalResultsWorkspacePage.review-status.test.tsx \
  src/components/tasks/reportReviewContract.test.ts \
  src/routes/frontier/FrontierLiveDemoPage.test.tsx \
  src/routes/NewTaskPage.drafts.test.tsx
```

验收报告必须写候选精确SHA、生产文件diff、命令/结果、固定与live标签、各类别误差/误给/误扣、区间分母、依据证据覆盖率、建议/强制处理/无分率、共享规则一致性、耗时和调用量。缺少观测填unverified/null，不填写0。每个失败应有原输入、预期依据、实际分数/trace、责任任务和复跑命令。
