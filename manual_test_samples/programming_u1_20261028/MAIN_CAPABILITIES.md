# U1：当前 main 编程能力核对

核对时间：2026-10-28 13:17 Asia/Singapore。范围为 U1 样本设计所需的静态代码与既有回归检查；本报告没有执行真实模型、真实 OCI、生产数据、产品上传或教师评分验收。

## 版本与证据边界

- GitHub `SmarTAI-2025/SmarTAI` 的 `main` 在本轮只读核对时为 **`101352c8a7df4e628d2716a2731b702d5f294a8d`**。后文链接全部固定到这个 SHA；后续 main 变更须重新核对受影响文件。
- 本机根目录 `main/HEAD` 为 **`7e2b7a4993c3e8850cdf9eb0261a9a2e8fc40484`**，提交日期 2026-08-26，不能作为当前主干能力依据。其工作区另有既存改动，本报告未修改。
- 本机缓存 `origin/main` 为 **`f8baad73f10475f4094eba23502393f3201107f6`**。下列文件通过 GitHub `fetch_file(ref=101352c8…)` 获得当前文件 blob SHA，再与 `git rev-parse f8baad73:<path>` 比对完全相同。因此可用这些不可变 git 对象作定位，不能把整个缓存版本称为当前 main。
- `backend/skills/base.py` 已在新 main 变化；平台故障分类使用当前远端文件读取结果，不能以旧文件相同为前提。

| 文件 | 当前 main blob SHA（与本机 f8baad73 对象一致） |
| --- | --- |
| `backend/skills/programming.py` | `e7d715eda7bd00d803fb2c5fdff8221ffede8f19` |
| `backend/tools/code_interpreter.py` | `ca3268e8ab34a86667fa44fcc1a0bf350c529807` |
| `backend/services/programming_test_contract.py` | `8747f207e62a21861124758833429de1c1837a65` |
| `backend/tools/oci_grading_runner.py` | `ab23da5a8ad3bf295effce3bf79d55f63371d44f` |
| `backend/agents/ingest_agent.py` | `d1116cd020f8c39ef95d8b4822956a15543fd1cc` |
| `backend/tools/file_processing.py` | `f1bf6b998e66025b87f72e02f17bb5b1bdc054e6` |
| `backend/services/question_matching.py` | `1bef7e087c3d3d30e81533b2ebf9f8b273aedd82` |
| `backend/models.py` | `f80b62cd1fd76fde091bcca423fd029b10c84cf0` |
| `backend/prompts/programming.txt` | `f0686dc03b46001666451838c06cad1dc6e78063` |
| `backend/tests/test_oci_grading_runner.py` | `bc716bfbee2e44691a1e865d66e2b17c97b7c81d` |
| `backend/tests/test_programming_prepared_function_cases.py` | `cd812f4afb7de0dc5452fd432dc25f241616f6ad` |
| `backend/tests/test_production_host_execution.py` | `6110d45fb4de7d9f9e5cede044b50ed65cf3d1f7` |

## 能力与 U1 样本安排

| 项目 | 当前代码证据 | 样本与验收边界 |
| --- | --- | --- |
| Python stdin | `run_sandbox` 将 `input` 交给程序，并比较实际输出；见 [code_interpreter.py L250–267](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/code_interpreter.py#L250)。 | 可准备完整 stdin 程序、纯文本输入及输出。当前比较会去除首尾空白；若教师要求严格空白，必须另列复核条件。 |
| Python 函数 | `TestCase` 有 `io_mode`、精确 `function_name`、JSON 参数列表和字符串 `expected_return`；实际执行由 `function_name` 是否存在分流。见 [models.py L126–167](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/models.py#L126)、[code_interpreter.py L280–369](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/code_interpreter.py#L280)。 | 明确函数名、签名、位置参数、返回类型；函数题预期返回值不能填作 stdout。当前比较存在数值容差；类型、精度、返回容器要求以样题规则为准。 |
| 共享 helper、函数乱序 | 已显式提供的函数测试直接把完整 `student_code` 与指定函数调用拼接；既有测试明确调用第二个函数，见 [test_oci_grading_runner.py L54–63](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tests/test_oci_grading_runner.py#L54)。但无准备测试时 `_detect_function_only` 仍取第一个顶层 `def`，见 [programming.py L183–200](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L183)、[L540–591](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L540)。 | 显式入口加完整源文件可作为基础样本；另保留 helper 在首位、答案函数乱序的压力样本。不能宣称任意源码多题映射已经端到端通过；不能由首个函数决定题目。 |
| 普通答案题号乱序 | 识别提示要求按唯一题号/内容映射，禁止按数组位置；确定性别名与冲突检查保留歧义，见 [ingest_agent.py L187–192](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/agents/ingest_agent.py#L187)、[question_matching.py L24–95](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/services/question_matching.py#L24)。 | 准备明确题号倒序、遗漏中间题、题号与函数冲突的材料；歧义项应保留源码、待复核。通用答案映射不等于已完成 Python 多函数 AST 映射验收。 |
| 缺交 | 空代码在 skill 中直接返回 0、置信度 1，且不运行沙箱或模型，见 [programming.py L516–538](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L516)。 | 仅教师确认确实未提交才适用 0 分建议。文件丢失、上传失败、识别失败或错误归属不得冒充缺交。 |
| 部分正确与代码错误 | 运行工具返回逐例结果、失败原因和通过数；评分仍由模型结合题干、规则、运行摘要生成，最终规范到题目满分，见 [programming.py L645–742](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L645)。 | 样本按明确分项规则建议分数，不从测试数量推导权重；语法错误、运行异常、逻辑错误分别列明。该代码证据不证明模型一定遵守每条教师规则。 |
| 不按通过率直接计分 | 当前提示仍要求教师测试与函数测试以 pass rate 为评分锚点，见 [programming.txt L19–31](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/prompts/programming.txt#L19)。最终代码读取模型分数，没有在该 skill 内直接用通过率乘满分，也没有固定 70/30 数值公式。 | 必须保留“多数重复普通用例通过，但违反高权重必需规则”和“测试仅覆盖部分分项”的反例。这里只确认提示存在偏向，通过率导致误扣的真实模型结果本轮未测；由 09 实现负责人验收评分语义。 |
| 标答与来源 | 当前 skill 会把 `reference_answer` 加入评分提示；已保存全 AI 测试保留 AI 分支，新生成测试强制 `llm_generated`，见 [programming.py L288–302](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L288)、[L561–571](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L561)、[L677–682](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L677)。 | 构造题目、构造提交、建议规则、AI 标答分别声明来源和未确认状态。不得因写入“教师资料”文件就把 AI 建议升级为教师金标准。 |
| 平台故障不得扣分 | 生产拒绝宿主回退；OCI 不可用/执行ID错误/未执行/基础设施错误抛出 `IsolatedRunnerUnavailable`，见 [code_interpreter.py L114–121](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/code_interpreter.py#L114)、[oci_grading_runner.py L142–201](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/oci_grading_runner.py#L142)。错误结果带 `error_kind=programming_execution_unavailable`；既有回归要求说明“未执行”，不得说成 0/N，见 [test_production_host_execution.py L25–34](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tests/test_production_host_execution.py#L25)。 | 平台故障情景使用“无有效建议分/待重试或人工复核”，不得把内部错误结果的占位 0 当学生成绩。平台 CLI 超时与隔离内学生程序超时分列。本轮仅准备故障情景，未中断任何实际服务。 |
| 运行环境 | 配置 `oci` 时使用独立 transport；生产要求 rootless、seccomp、资源控制及固定镜像，见 [oci_grading_runner.py L82–100](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/oci_grading_runner.py#L82)。开发/测试仍可走宿主子进程，源码明确它不构成恶意代码隔离。 | 本机人工检查的构造样例可离线做结果核算；必须标注本机验证与实际 OCI/产品运行的区别。不得用本机 smoke 声称生产隔离验收完成。 |
| C/C++ 单文件 | 当前 skill 的语言门禁清空非 Python 测试，仅代码审查；执行工具也只支持 Python，见 [programming.py L639–643](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L639)、[code_interpreter.py L233–235](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/code_interpreter.py#L233)。 | 可随后提供 C/C++ 单文件 stdin 题与提交。当前 main 不能写成已编译运行；本机编译即使成功，也只验证样本，不证明产品已经接通。复杂类、多文件和跨语言函数 ABI 不纳入 U1 首包。 |
| 上传与纯文本 | 文件识别按内容检测；可读源码 bytes 可走文本解码，不应仅根据旧扩展名清单认定不能读 `.py/.c/.cpp`，见 [file_processing.py L144–208](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/file_processing.py#L144)、[L580–606](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/file_processing.py#L580)。 | 样本保留原始源码及独立纯文本题目/测试资料；实际浏览器扩展名、MIME、保存刷新再执行全链路留待 U2，不由此静态审计宣称通过。 |

## 应保留的最小边界样本

1. 同一 Python 完整文件的 helper 在第一位，两个作答函数顺序互换，指定入口与预期题号保持不变。
2. 教师规则包含高权重边界/效率项，用例数目不等于分值权重；例如多个重复普通输入不能冲淡一个高权重边界项。
3. 缺交（教师已核实）与上传/识别未完成分别记录，后者不得建议零分。
4. OCI 不可用、执行合同无效与学生语法/运行错误分别记录；平台故障没有有效通过率或有效分数。
5. 对空 stdout 有严格要求时额外复核：当前 [code_interpreter.py L265](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/tools/code_interpreter.py#L265) 仅在 `expected_output` 非空时比较，不能把空预期的正常退出直接当作语义正确。此为静态代码边界，未修改实现。
6. 多个 fenced code block 的完整性复核：当前 `_extract_code` 取首个围栏块，见 [programming.py L154–160](https://github.com/SmarTAI-2025/SmarTAI/blob/101352c8a7df4e628d2716a2731b702d5f294a8d/backend/skills/programming.py#L154)。首包执行材料采用单个完整源码文件；拆散 helper 的跨块材料是后续映射验收输入，不能静默丢块或自行修复学生代码。

## 已有回归与可复用材料

- `backend/tests/test_programming_prepared_function_cases.py`：构造 `stable_softmax` 未做数值平移的提交，空列表/普通输入通过而 `[1000,1000]` 溢出；模型输出由 `MagicMock` 固定为 5 分。这个 **5 分不是教师金标准**，也不是实测模型评分。可作为补充构造素材线索；正式 U1 需另写明确规则、标答和待确认分项。
- `backend/tests/test_oci_grading_runner.py`：运行命令由 fake `_command` 替换，验证函数入口保持、错误合同、清理、容量与隔离参数。它是模拟 transport 回归，不能作为本轮真实 OCI 运行证据。
- `backend/tests/test_production_host_execution.py`：验证生产模式拒绝宿主执行，并将不可用标作失败专家而非学生测试失败。
- `backend/tests/test_submission_question_matching.py`：普通答案的题号匹配/冲突回归；不能直接替代多函数源码映射验收。

## 本轮已运行的最小离线回归

在 `/private/tmp/smartai-u1-main-20261028-01a180ed` 的固定 SHA `101352c8…` 文件上，使用 `/opt/anaconda3/envs/smartai/bin/python`（Python 3.13.13）运行下列选择集：**36 passed in 1.02s，exit code 0**。运行前确认目录没有 `.env`。实际 Python 审计钩子拒绝 socket 连接、SQLite 连接和真实子进程启动；**阻止事件数为 0**，即本轮没有尝试这些动作。

这 36 项覆盖函数名现状（包含“返回首个 def”的既有断言）、值比较、语言检测、测试数据模型转换、模拟 OCI transport 合同与生产宿主执行拒绝。**首个 def 断言通过证明该旧行为仍存在，并非函数乱序验收通过。** 未运行真实模型、真实 OCI、学生程序、数据库或产品上传流程。

`--noconftest` 是本轮特意选择的狭窄验证边界：仓库 `backend/tests/conftest.py` 有自动创建/恢复 SQLite 的 fixture，为遵守本轮不使用数据库的限定而跳过；所选测试不依赖它。不能把这次结果写成正常配置的后端全套回归。第一次收集因临时快照未展开 `tools/runner_spike` 而停止（`ModuleNotFoundError: tools`，未执行任何测试）；从同一 HEAD 展开该目录后上述运行通过，没有改实现或断言。

实际命令（在上述临时快照目录执行；两个 key 值均为专用假值）：

```bash
env -i PATH=/opt/anaconda3/envs/smartai/bin:/usr/bin:/bin \
  PYTHONPATH=/private/tmp/smartai-u1-main-20261028-01a180ed \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  SMARTAI_RUNTIME_ENVIRONMENT=test SMARTAI_DATABASE_HEAVY=false \
  SMARTAI_DATABASE_URL=sqlite:///:memory: SMARTAI_DATABASE_AUTO_CREATE=false \
  SMARTAI_SEED_TEST_USERS=false SMARTAI_PROGRAM_RUNNER_MODE=disabled \
  SMARTAI_PROVIDER_ENCRYPTION_KEY=u1-offline-test-key-0123456789abcdef \
  SMARTAI_JWT_SECRET=u1-offline-jwt-secret-0123456789abcdef \
  /opt/anaconda3/envs/smartai/bin/python - <<'PY'
import sys
import pytest
blocked = []
def audit(event, args):
    if event in {'socket.connect', 'sqlite3.connect', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        blocked.append(event)
        raise RuntimeError('U1 offline guard rejected ' + event)
sys.addaudithook(audit)
args = ['-q', '-c', '/dev/null', '--noconftest', '-p', 'pytest_asyncio.plugin', '-p', 'no:cacheprovider',
 'backend/tests/test_programming_function_only.py::test_detect_function_only_yes',
 'backend/tests/test_programming_function_only.py::test_detect_function_only_no_when_print_present',
 'backend/tests/test_programming_function_only.py::test_detect_function_only_no_when_input_present',
 'backend/tests/test_programming_function_only.py::test_detect_function_only_returns_first_def',
 'backend/tests/test_programming_function_only.py::test_values_match_int_vs_str',
 'backend/tests/test_programming_function_only.py::test_values_match_list_normalization',
 'backend/tests/test_programming_function_only.py::test_values_match_float_tolerance',
 'backend/tests/test_programming_function_only.py::test_values_match_mismatch',
 'backend/tests/test_programming_prepared_function_cases.py::test_function_return_float_tolerance_preserves_structure',
 'backend/tests/test_programming_skill.py::test_detect_language_python',
 'backend/tests/test_programming_skill.py::test_detect_language_cpp',
 'backend/tests/test_programming_skill.py::test_coerce_test_cases_from_dicts',
 'backend/tests/test_programming_skill.py::test_coerce_test_cases_from_models',
 'backend/tests/test_oci_grading_runner.py',
 'backend/tests/test_production_host_execution.py']
print('PYTEST_ARGS:', repr(args))
code = pytest.main(args)
print('OFFLINE_GUARD_BLOCKED_EVENTS:', blocked)
print('EXIT_CODE:', code)
raise SystemExit(code)
PY
```

`test_programming_prepared_function_cases.py` 的完整实际子进程测试、`test_submission_question_matching.py` 的完整集成本轮未运行；可作为 U2 后续固定环境的候选回归，不从本轮 36 项外推。现有回归与 U1 样本自校验是两类证据；U2 的真实模型质量、现有页面流程和实际 OCI 验收仍未执行。
