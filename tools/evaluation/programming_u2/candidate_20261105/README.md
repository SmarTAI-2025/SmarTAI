# U2 候选版本验收

用同一批合成输入复测两处跨层行为：教师在原页面编辑或确认后，数据库和执行器采用什么值；批改中断后，真实执行器会重跑哪些用例。10 月的 U2 基线仍可按提交 [344d0cfb](https://github.com/SmarTAI-2025/SmarTAI/tree/344d0cfba355139b97f157bd4424042b4250fd77/tools/evaluation/programming_u2) 查阅。

| 固定输入与动作 | 验收条件 |
|---|---|
| 原页面将 fib(0) → 0 改为 fib(5) → 5 并保存，再改为 fib(9) → 34 并取消 | 只发出一次 PUT。刷新后仍是5，取消前后的版本相同。挂载 API 接收合成的 `.py` 作答，原生识别保留全文且模型调用为0。批改从 SQLite 读取参数，实际运行并保存返回值5。 |
| 平台执行失败，点「人工继续处理」，随后尝试进入分析并确认空分数 | 页面阻止进入分析，最终分输入为空，点确认时提示手动给分且任务写请求为0。固定评分调用为0。 |
| 仅 AI 用例，固定评分回复6；另用教师用例对照，回复0 | 暂定6分在分析前要求复核，明确确认后数据库门禁放行。有效0分可直接进入分析。 |
| `add(2,3) → 5` 已完成，`add(3,4) → 7` 随后遇到平台故障 | 首轮缺少完整结果。重试只运行第二条，最终按原顺序取回2/2。 |
| 两条 add 用例的真实完成回调都返回后，取消评分任务 | 恢复时新增执行次数为0，取回原用例编号、顺序与2/2结果。 |

## 运行

需要候选版本的 Python 开发依赖、前端锁定依赖、Node 和 Playwright Chromium。设置 `CANDIDATE` 为待验收的干净候选目录，`U2` 为此 README 所在目录。先在候选的 `frontend/app` 构建面向同源 API 的前端，再运行工具。

```sh
(cd "$CANDIDATE/frontend/app" && VITE_SMARTAI_BACKEND_URL=/api npm run build -- --mode e2e)
python "$U2/accept.py" "$CANDIDATE"
```

`--only browser` 运行4个真实 Chromium、回环 HTTP、临时 SQLite 场景。`--only retry` 运行2个恢复场景，只需 Python 依赖。工具为每轮生成临时签名密钥，使用固定评分回复；执行的 Python 源码限于脚本内的合成示例。恢复场景使用内存事件仓库和候选的实际 `run_sandbox`。退出码0表示所选断言均通过。

## 覆盖来源与实测

PR286 已有下列回归。表中链接固定到核查时已推送的 [d36b3f07](https://github.com/SmarTAI-2025/SmarTAI/commit/d36b3f073413112f62475ebdabf61d6be97b7b0a)。

| U2 核查点 | 已推送的对应测试 |
|---|---|
| D01 编辑后的执行、D02 无效输入回滚 | [保存与执行](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_saved_grading_flow.py)、[接口合同](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_test_contract.py) |
| D03 空预期、D05 大整数比较 | [比较器及执行边界](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_runner_comparisons.py) |
| D04 混合来源、暂定分和发布、专家间复用 | [评分/发布合同](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_scoring_contract.py)、[准备与执行次数](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_run_evidence.py) |
| D06 非有限耗时、D07 清理失败后限制容量 | [OCI 消息与清理合同](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_oci_grading_runner.py) |
| 单函数、乱序、helper 与整份源码保留 | [上传与题目映射](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_source_answers.py) |

PR286 分别测试了[调度器完成回调](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_runner_dispatch.py)和传入替身执行器时的[部分结果恢复](https://github.com/SmarTAI-2025/SmarTAI/blob/d36b3f073413112f62475ebdabf61d6be97b7b0a/backend/tests/test_programming_run_evidence.py)。本目录把批改服务接到真实调度器，直接断言重试时实际运行的输入。浏览器的保存与确认也直接写入挂载的产品 API。

Mac 本地在候选 `062c3e7e2cfcb1f2954ca252b86f06022ad4af70` 首次完成6/6，自审后在 `00c98a5ebc55df82a19a9da9d9616e72b4e619fc` 再次完成6/6，包含学生源码 `.py` 上传。Fibonacci 源码与 U1 `c1c36025` 的 `S01_fib_correct.py` 逐字节相同。旧候选 `0e7f451ec42f83b0c9148844df6f7f336dd8efff` 的负例仍能触发断言：重试重复运行了已通过的 `[2,3]`。更早版本的格式结果及模拟页面证据保留在当时的记录中。实际模型调用和真实 OCI 调用均为0。
