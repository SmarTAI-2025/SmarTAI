# U2 补充审核（2026-10-28）

本次补齐主审核发现的观察与清单遗漏，仍是第一阶段基线。仅修改本目录工具、报告与证据；不实现同学的07/08/09或新增三项，不改产品识别、复核、其他题型、前端设计或共享执行器。

## 固定版本与本次结果

- 产品 main：`101352c8a7df4e628d2716a2731b702d5f294a8d`；backend tree `399ebfb4a8258bc1f5cb11288810445fac598a88`。实跑checkout HEAD为首轮U2提交`4ac06c80299196d94d6a490984ebe03f5329072f`加本目录补审改动，产品源码无diff。最终脚本hash另存证据manifest，不能仅凭HEAD推断未提交脚本版本。
- U1继续冻结`c1c36025d2dad7c8f3208f3ba0fdb4dd2acf24bb`，见[输入manifest](INPUT_MANIFEST.json)。新双参数/入口/import常量样本属于后续独立版本；本轮不静默混用，也不将AI建议当教师金标。
- Mac本地：22项技能/比较、10项HTTP/SQLite、9项浏览器、2项fake-command合同观察，共43项；观察完成含缺陷成功复现，不代表43项产品验收通过。原63项后端回归再次通过，7项按原边界排除；0防线拒绝事件。
- 真实模型调用、OCI执行、学生代码执行均0；未访问生产数据。任何耗时都是本地探针墙钟，不是整班真实耗时或计费依据。
- [证据与脚本hash索引](evidence/followup-20261028/manifest.json)保存准确输入/工具版本及43项观察计数；[输入漂移负例](evidence/followup-20261028/input-drift-check.json)验证冻结材料改变时拒绝继续。

## 新增反例与明确未测项

| ID | 当前观察 | 证据与界限 | 后续关闭条件 |
|---|---|---|---|
| D04 | 混合1 AI+4教师函数用例，runner输入来源正确；评分请求却为`SANDBOX_RAN_TEACHER_CASES`，结果尾注称5个教师用例 | [技能观察](evidence/followup-20261028/programming.json) B21；固定runner报告/固定模型回复，真实技能拼接 | 评分请求与最终说明分来源准确计数，保留原来源、预期、难例 |
| D05 | `2**53`与`2**53+1`、对应负数被比较器判相同；小整数不等和相同整数对照正常 | 同上B22；实际纯函数，无执行 | 整数精确比较，浮点容差按合同；bool/int严格区分目前仅是合同建议，不另定bug |
| D06 | `duration_ms=Infinity`固定执行响应被真实协议/适配接收，结果passed | [runner观察](evidence/followup-20261028/runner.json)；fake command | 非有限指标拒绝，不形成有效执行证明，错误归属平台 |
| D07 | run超时且rm失败，两次调用都进入fake run；`max_concurrent=1`的槽位在清理未成功时释放 | 同上；真实槽位逻辑+故障注入，无实际容器，不声称生产泄漏 | 清理失败有有界隔离/核销机制；真实OCI再验资源、残留和恢复 |

D01–D03仍复现，没有修复产品。D06–D07属于08合同和安全验收缺口；共享runner也被数学工具使用，未来修复必须检查相邻调用路径。U2只保留反例，不修改共享业务。

## 页面、保存及评分状态

- 未保存/取消：原stdin控件中改为`9\n9\n → 18`，取消后刷新重开恢复已保存`5\n7\n → 12`；PUT次数1→1、版本2→2。证明界面没有把草稿发给固定API；不冒充实际runner验证。
- 首轮浏览器fixture错误地对所有情形返回`ready_for_confirmation=true`。本轮平台失败fixture与真实HTTP/SQLite已观察状态一致：最终score/provisional_score均null、1项必须处理、不可确认。原界面阻止进入结果分析，留在复核并提示缺少有效分数。新增明确空分数和确认阻断断言；旧fixture记录保留为历史，不能用作发布门禁证据。
- 平台失败详情显示“无有效分数”，最终得分输入为空；点击“全部确认”提示需手动输入有效分数，任务写请求3→3，未发出保存/确认/发布请求。[确认阻断截图](evidence/followup-20261028/execution-failure-confirmation-blocked.png)、[分析门禁截图](evidence/followup-20261028/execution-failure-review-gate.png)。
- 真实临时SQLite补充状态对照：明确有效的0分保存为0；有分数6的低置信建议仍保留6且可确认；显式平台all_failed保存为null并阻断。前两者的分数/标记由固定batch注入，只证明保存与门禁合同。不要把建议复核自动扩成硬阻断，也不能拿内部专家0占位当最终学生0分。
- [浏览器观察](evidence/followup-20261028/browser.json)、[HTTP/SQLite观察](evidence/followup-20261028/workflow.json)分别报告；没有连接真实前后端跑完整模型/OCI链路。

## 探针自身审阅与回归范围

技能探针校验冻结输入hash，记录实际接收源码hash、每个runner报告及来源字段、截获的评分请求摘要，检查请求次数与报告次数一致、固定分数未被意外异常吞掉。当前缺陷的行为断言若变化会失败并要求重新审核，不靠“文件写出”宣称完成。输入漂移负例已验证在业务导入前拒绝且不写观察文件。

HTTP探针补充上传started、函数字段读回、准确题号分数、平台失败null/成功兄弟保留、异常用例读回的明确判据。浏览器仍拒绝未知API、外网和pageerror；真实构建index/JS与服务字节一致，页面状态和接口请求均留证据。

非编程路径：本次对`backend/`、`frontend/`、`tools/runner_spike/`零diff；未修改识别、复核、计算/证明/概念/客观题行为。63项既有回归包括共享OCI命令合同、生产禁宿主保护、执行错误投影和知识引用；它不是其他题型的完整质量验收。无变化的全量沿用同main的U3 [PR279](https://github.com/SmarTAI-2025/SmarTAI/pull/279) / `b1c7315849ca55b997c55d88802be2d4c0ba1430`：7464后端通过/101条件跳过、1556前端通过，不重跑、不重复计数。真实PostgreSQL/OCI等跳过不转为通过。

## 复跑与后续验收

技能、HTTP、浏览器按[README](README.md)运行，后端63项按[VALIDATION](VALIDATION.md)原选择运行。另执行以下两项固定命令观察：

```sh
/opt/anaconda3/envs/smartai-py312/bin/python tools/evaluation/programming_u2/run_offline.py --repo . -q -s tools/evaluation/programming_u2/runner_contract_probe.py --junitxml=/private/tmp/u2-repeat-runner.xml
```

清单扩至36项：四来源统一适配、纯文本双参数与入口合同、未保存/取消、并发一次准备、失败/空结果与有界重试、教师修改与旧结果晚到保护、混合来源、比较语义、评分状态及清理故障。统一适配与准备生命周期尚未由同学交付，明确待验收；顺序skill调用和普通409不能替代这些并发合同。真实模型质量、真实OCI、人工评分参考及整班调用/耗时/费用仍未验收。
