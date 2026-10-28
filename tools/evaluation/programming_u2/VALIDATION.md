# U2 验证记录

## 固定输入与版本

- 产品源：`101352c8a7df4e628d2716a2731b702d5f294a8d`，tree `e2e80a85e87548e044042a791b713eafc19ed209`。
- U1 v1：[PR277](https://github.com/SmarTAI-2025/SmarTAI/pull/277)，head `c1c36025d2dad7c8f3208f3ba0fdb4dd2acf24bb`。采用该版本 `manual_test_samples/programming_u1_20261028`，消费文件hash在本目录证据内。6题/32判分行；全部人工确认空白，真实完整编程集0。本轮没有覆盖或修改U1材料。
- U3：同一产品源，通用跨页/块/材料及暂停恢复由 [U3独立任务](codex://threads/01a180f0-3f9a-76d1-92ab-1f3b20ce1c67) 维护。引用其恢复/映射报告，U2不再次开发或重复登记缺陷。M1公共定义跨块未携带、M2同号歧义材料未留未决问题由U3记录并归07。
- 本轮 Mac arm64；Python 3.12.14，U3逐项验证 `requirements-dev.txt` 的100个适用依赖、0版本差异。U2浏览器Node26.10.0 / Playwright1.61.1；仓库目标Node22.16.0，单独引用U3的Node22.22.2全前端回归，不把版本混为同一次运行。
- 前端依赖lock SHA256：`7ae4c47ef046edf34986bd07bd7d7ad7bb9e6e85223d43d5e49cb74e3c900dea`。服务源码与构建字节核对见 [serve-provenance.json](evidence/serve-provenance.json) 和最终 [browser.json](evidence/browser.json) 的 `served_build`。未安装新依赖。

## U2 本轮结果

| 检查 | 结果 | 原始证据 |
|---|---|---|
| 技能/比较/OCI启动前拒绝独立观察 | 20项完成，固定模型/runner、0真实执行 | [JSON](evidence/programming.json)、[日志](evidence/programming-probe.log) |
| HTTP/真实SQLite独立观察 | 9项完成，含跨进程读回、正常/失败批改结果入库 | [JSON](evidence/workflow.json)、[日志](evidence/workflow.log) |
| 浏览器独立观察 | 8项完成，包含D01缺陷复现与4类固定结果；未知请求、外网请求、pageerror均0 | [JSON](evidence/browser.json) |
| 编程/执行合同既有后端回归 | 63通过、7明确排除：5需宿主子进程，2为不属本轮的计算题符号执行参数 | [JUnit](evidence/programming-regression.xml)、[日志](evidence/programming-regression.log) |
| 既有编程资料浏览器回归 | 14通过，含桌面/手机、中英文 | [日志](evidence/browser-materials.log) |
| 既有工作流/归属浏览器回归 | 14通过，含上传进度、下一阶段、历史重开、归属保存刷新 | [日志](evidence/browser-workflow.log) |
| 前端构建及E2E类型 | 本轮构建、E2E脚本自带类型检查通过 | [构建日志](evidence/browser-build.log)及上述浏览器日志 |

37个独立观察是记录到的场景数，其中有已复现缺陷；不是37个产品验收通过。63/28项回归不与独立观察或U3全量简单相加。

平台失败补充：对显式 `all_failed` / `programming_execution_unavailable` 固定批改结果，真实运行/结果存储路径得到 `partial_failed`、最终 `score=null`、需要复核且禁止确认发布。最终浏览器失败fixture也改用这一已观察到的null分数形态。它证明失败结果的保存/显示合同，不证明真实OCI已经运行。内部专家的0分占位不应冒充最终学生0分。

## 可复跑的后端选择

在仓库根目录运行（路径可替换，输出使用新临时目录）：

```sh
/opt/anaconda3/envs/smartai-py312/bin/python tools/evaluation/programming_u2/run_offline.py --repo . -q \
  backend/tests/test_programming_skill.py \
  backend/tests/test_programming_function_only.py \
  backend/tests/test_programming_logs_null.py \
  backend/tests/test_programming_prepared_function_cases.py \
  backend/tests/test_oci_grading_runner.py \
  backend/tests/test_production_host_execution.py \
  backend/tests/test_grading_execution_error_projection.py \
  backend/tests/test_calculation_programming_knowledge.py \
  --deselect=backend/tests/test_programming_function_only.py::test_sandbox_function_call_mode \
  --deselect=backend/tests/test_programming_function_only.py::test_sandbox_function_call_fibonacci \
  --deselect=backend/tests/test_programming_function_only.py::test_programming_skill_zero_pass_rate_caps_confidence \
  --deselect=backend/tests/test_programming_prepared_function_cases.py::test_prepared_generated_cases_call_function_and_preserve_real_failure \
  '--deselect=backend/tests/test_calculation_programming_knowledge.py::test_selected_textbook_is_reference_only_and_has_system_citations[False-calculation]' \
  '--deselect=backend/tests/test_calculation_programming_knowledge.py::test_selected_textbook_is_reference_only_and_has_system_citations[True-calculation]' \
  --junitxml=/private/tmp/u2-repeat-regression.xml
```

OCI相关既有测试在这里是 fake transport合同测试，不是真容器。首次探索日志保留（提交版仅去除行尾空白）但不纳入最终通过数；未改变产品断言或配置来掩盖失败。

浏览器既有回归在 `frontend/app` 运行：

```sh
SMARTAI_E2E_FRONTEND_URL=http://127.0.0.1:5188 SMARTAI_E2E_BACKEND_URL=http://127.0.0.1:8188 npm run e2e -- e2e/question-analysis-materials.spec.ts --output=/private/tmp/u2-repeat-materials
SMARTAI_E2E_FRONTEND_URL=http://127.0.0.1:5188 SMARTAI_E2E_BACKEND_URL=http://127.0.0.1:8188 npm run e2e -- e2e/workflow-navigation.spec.ts e2e/answer-assignment-review.spec.ts --output=/private/tmp/u2-repeat-navigation
```

## 全仓库、本次代码审阅与交付

同一 main/tree 的全仓库 Mac 本地回归由 U3 执行并共享，U2读取最终 JUnit/日志核对并保留[汇总及原件 SHA256](evidence/u3-shared-regression.json)：后端 **7464通过、101跳过、0失败/错误**（644.10秒）；前端 **1556通过/151文件**，类型检查、scope检查、公开构建均通过。后端跳过为 PostgreSQL 91、真实Linux OCI 3、Linux资源限制3、真实S3 3、显式full-book容量1；这些均是未测，不算通过。全量覆盖原产品源码，与U2子集不重复相加；不包含新增U2观察工具。U3之前的环境校准/中断运行不纳入最终数字。

新增工具经过独立审阅及主任务复核：畸形用例按实际读回/校验结果判定；浏览器未知API失败而非静默成功；服务index/JS字节核对；任何被吞掉的禁网/禁进程拒绝均不得算干净的离线通过。最终重新运行了修改后的探针。产品源码未修改。

用户授权本地审核测试、提交推送PR，不合并；仓库自动Actions/Pages预览允许，仍不等于生产部署或接受。最新main兼容性、提交/PR与CI状态以PR及canonical导航事件的独立字段为准，静态证据中的main SHA不随之后CI或文档提交改变。
