# U1 V2 样本来源工具

本工具整理了制作 U1 样本时使用的 Python 生成逻辑。原始 V1 制样器和 V2 增补器的题目定义、提交模板、定点修改和索引构造保存在 [fixture_sources.py](fixture_sources.py)。[generate.py](generate.py) 负责写入用例，并渲染规则和判分表。以提交 `ab36293b5b00ea2dd20803cd89c727398671c835` 中的[冻结样本包](../../../manual_test_samples/programming_u1_20261028/)为准。从仓库根目录运行以下命令，最后一个参数应为父目录已存在、位于样本包外的新目录。

```sh
python -m tools.evaluation.programming_u1_sources.generate ./u1-derived
```

| 原工作 | 来源与复现方式 |
| --- | --- |
| 编写题目、规则、标答和独立提交 | 按原制样定义重建 9 份标答、29 个提交源码和 3 份 JSON 索引。索引保留由同一源码产生的多题判分记录、实际缺交、分项建议分和未评分情境。 |
| 制作用例和变体 | 从内存中的题目定义生成 88 个输入及预期文件。29 个提交中有 10 个由标答或共享源码替换片段产生。生成 4 份乱序和纯文本映射材料，保留同一输入在不同题目的调用契约。 |
| 生成规则说明、判分表 | 按题目和提交 ID 关联评分草案与冻结的 V2 运行记录，生成 `QUESTIONS_AND_RULES.md`、`MANUAL_GRADING_REVIEW.csv` 和 `MANUAL_GRADING_REVIEW.md`。建议分取自原草案，六个人工确认栏留空。 |
| 验算执行结果 | 样本包内的 [validate_samples.py](../../../manual_test_samples/programming_u1_20261028/validate_samples.py) 负责执行构造样本并生成事实报告。本工具唯一需要读取的冻结输入是 `validation/local_fixture_results_v2.json`，用于照录判分表中的执行事实。 |
| 打包 | 原打包操作使用 SHA-256 清单、标准 ZIP 写入及回读比对。仓库自带的 `git archive` 可从冻结子树导出相同的 147 个成员。解包后在样本目录运行 `shasum -a 256 -c SHA256SUMS` 可核对成员内容。压缩器及归档元数据会影响 ZIP 自身的字节和哈希。 |

验证命令如下。生成回归只提供冻结的 V2 运行报告，按冻结的校验和逐个核对全部 136 个生成文件。另外 11 个冻结成员为说明文档、版本清单、校验和、现有校验器及其测试、两份历史验算报告，可从冻结子树直接取得。

```sh
python -m pytest -q tools/evaluation/programming_u1_sources/test_generate.py
```
