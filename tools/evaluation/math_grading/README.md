# 数理批改评测（任务4）

4A 已提供：40个合成样例、旧 main 固定响应回放、评测器、公开数据集核验。4B已完成候选 `72377b72` 的本地集成验收：40例符合预期（38有效分数、2故障无分），旧11处分差消除。[最终报告](reports/FINAL-72377b72.md) 汇总实际工具、后端、前端及公开合同证据；提交状态以canonical HANDOFF-T4为准。

共同合同位于 canonical `docs/project_navigation/2026-10-20-math-grading/CONTRACT.md`。本目录不实现评分计划、数学核验器或生产评分引擎。

## 数据与证据边界

`corpus.py` 含27个数学、13个物理样例：28计算、6证明/推理、6概念。每项记录原题、原标准、学生原文、预期区间、理由、独立固定响应、可选核验请求。所有区间来自本任务按显式规则构造的预期，**没有教师已确认标签，也没有真实手写图片**。保底/扣分政策只对写明该政策的样例生效。

固定响应刻意给出可用评分，以隔离生产代码对分数的覆盖行为。旧版回放实际执行原 `CalculationSkill/ProofSkill/ConceptSkill` 和 `numerical`；替换结构化模型调用和空KB检索。无参考答案时，显式注入参考生成失败；C26注入数学工具unknown，C27注入工具timeout；K05/K06注入调用/解析失败。它不覆盖真实参考代码生成、模型推理、OCR、run保存、多专家完整汇总或前端。网络连接被禁止。

`reports/baseline-main.json` 记录输入语料哈希、生产基线、提示词哈希、实际捕获提示、原始响应、分数/步骤、运行环境、耗时、调用次数及逐类指标。固定响应调用数不等于真实provider调用数。`reports/BASELINE.md` 给出可读结果。**回放通过不是AI准确率提升。**

未观测指标为null并带observed/total；不会把缺失证据当作通过。原文对齐仅检查quote确实出现，不冒充语义正确性验证；聚合重复扣分指标是error_id去重，4B另用定向测试检查重复规则/原文应用和虚构依赖。不承诺能自动识别全部语义等价错误。真教师区间指标保持null、样本数0。工具状态不能直接算分。

## 可复现命令

从包含本提交的仓库根运行：

```bash
/opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/math_grading_evaluation -q
/opt/anaconda3/envs/smartai/bin/python -m tools.evaluation.math_grading.run \
  --mode legacy-replay --output /private/tmp/math-baseline-replay.json
```

旧版适配器只接受生产代码保持 `0304ff2e0cc294ada51c99af8d5ab07540cb61a8` 的checkout。候选生产代码不同会拒绝运行，避免把旧版适配称为新引擎。测量命令退出0仅表示报告写出，必须查看失败案例；pytest验证的是评测器。

任务2工具集成后可单独执行：

```bash
/opt/anaconda3/envs/smartai/bin/python -m tools.evaluation.math_grading.run \
  --mode verify --output /private/tmp/math-verification.json
```

模块缺失退出2，不跳过并计通过。该模式验证16个请求，不能代替整题评分。对于定义域/常数完整性，refuted或unknown均可安全表示不能证明；要求可靠支持的常量/容差案例则明确要求supported。

## 4B接入

`candidate.py` 使用生产 `grade_batch`、评分计划编译器和数学工具；`candidate_fixtures.py` 只提供模型边界的独立固定响应，不读取expected。运行中的实际plan/claims/evidence来自生产trace。C01/02/03映射为同题不同作答，另有真实批次测试验证先冻结一次再处理三位学生。常规40例各自运行以便故障隔离，报告的计划调用数因此包括每例新run，不可冒充全班共享后的真实模型费用。

适配器约定：`async evaluate(case: dict, provider) -> dict`。`candidate-replay`传provider=None且禁止网络；live传有调用上限的provider。返回`id,score,max_score,steps,duration_ms,provider_entry_calls,fixed_response_calls`，失败score=None；另记录实际plan和trace。建议/强制处理取真实grade_batch的Correction经共享grading_review投影；这层Correction是存储之前的内部结果，不能将其raw_output误称公开API输出。候选live已有入口但未运行；收费开关仍有效。

```bash
# 在已交接的候选工作区运行。
/opt/anaconda3/envs/smartai/bin/python -m tools.evaluation.math_grading.run \
  --mode candidate-replay \
  --adapter tools.evaluation.math_grading.candidate:evaluate \
  --output /private/tmp/math-candidate.json
/opt/anaconda3/envs/smartai/bin/python -m tools.evaluation.math_grading.run \
  --mode compare --before tools/evaluation/math_grading/reports/baseline-main.json \
  --after /private/tmp/math-candidate.json --output /private/tmp/math-comparison.json
```

比较拒绝不同语料哈希或案例集，不把失败None替换为0。集成验收门和明确的失败归属在 `ACCEPTANCE.md`；不能以40个技能例替代产品回归。

## 真实模型入口（未运行）

现有base可对指定文本案例调用真实生产skill；不启用自动下载、不读取`.env`、不选择账号已存密钥。密钥只读取显式环境变量 `MATH_GRADING_EVAL_API_KEY`，不保存到报告。命令没有确认短语或`--enable-live`即拒绝。下面只给命令模板，本轮没有设置凭据或发送收费请求：

```bash
# 人工在本地设置专用环境变量后，替换已授权的 provider/model。
/opt/anaconda3/envs/smartai/bin/python -m tools.evaluation.math_grading.run \
  --mode live --enable-live --acknowledge-cost I_ACCEPT_PROVIDER_CHARGES \
  --provider openai --model YOUR_AUTHORIZED_MODEL \
  --case-ids C01,C03,C11 --max-cases 3 \
  --max-provider-calls 6 --max-output-tokens 2048 \
  --output /private/tmp/math-live.json
```

默认最多3例/6次provider入口，每次输出上限2048tokens。模型价格和SDK内部重试可能影响实际账单，**入口上限不是费用金额或HTTP请求数上限**；真实费用保留null，token缺失也不补0。若需硬费用封顶，应使用有额度限制的专用凭据。实时记录模型、实际请求、token（提供时）、耗时和已完成案例。故障注入例不允许live选择。取消/关闭前未执行此命令就不会发请求。

新主链live需同时指定 `--adapter tools.evaluation.math_grading.candidate:evaluate`；本轮未运行live。真实模型结果与固定回放分开保存、分开汇报。
