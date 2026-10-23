# 公开手写数据核验（2026-10-20）

检索与许可核验使用原始发布者资料。本轮仅查资料，没有下载学生图片/大压缩包，没有联系作者，也没有导入第三方样本。下表的“人工”说明标签来源，不代表已逐条验证标注质量。当前40例全为本任务合成文本，不能称为手写或教师金标准。

| 数据 | 可验证内容与标签 | 许可/可获得性 | 本任务用途 |
|---|---|---|---|
| EDU-CIRCUIT-HW，Georgia Tech团队 | 真实大学电路作业；1334例的observation/test切分；作答图、rubric、官方评分、部分人工纠正转写 | 官方发布入口存在；本次图书馆页返回403；数据包许可未核实。代码仓库Apache-2.0不能自动视为数据许可。题干因教材版权未随包发布 | 最接近物理评分目标的候选；题干与数据授权未闭环前不导入 |
| DrawEduMath，Heffernan WPI Lab | 2030张真实数学作答图、教师描述与QA；另有模型生成QA | CC-BY-NC-4.0，并指向ASSISTments负责使用规范；官方数据卡和CSV结构可读 | 可评理解/OCR；没有声明数值分项评分，不作为评分金标准 |
| MathWriting，Google Research | 提示采集的手写表达式轨迹/转写 | 数据CC-BY-NC-SA-4.0；代码Apache-2.0；官方提供全量与小样本链接 | 仅识别，不是自然学生解题/教师过程分 |
| CROHME 2023，竞赛作者 | InkML、PNG、MathML/LaTeX及符号关系图，混有人工合成训练数据 | Zenodo公开1.8GB包；抓取页面未显示明确license值，本轮未解压核验 | 仅表达式识别；无教师评分，不纳入评分回归 |
| UC Irvine手写微积分研究 | 真实作答，TA分数比较和独立审核 | 已核实论文说计划未来发布clean/noisy benchmark；本次未核实公开可下载且获许可的评分集 | 作为研究线索；不把论文图表或AI分数造为人工标签 |
| Mendeley Data `sf3kvjwknt/1`，Dinesh K P | 50名大学Data Science学生；题目/答案、原始与教师批阅PDF、逐题人工分数CSV | 发布页明示CC-BY-4.0及匿名化流程；2026-04-14 v1；文件可用性只核实到发布页，未下载逐条检查 | 标签结构适合后续评分流程；学科不是本轮数学物理主样本 |

EDU-CIRCUIT-HW 原始资料：[官方仓库](https://github.com/gt-learning-innovation/CIRCUIT_EDU_HW_ACL)、[发布说明](https://github.com/gt-learning-innovation/CIRCUIT_EDU_HW_ACL/tree/main/digital_library_release)、[数据DOI](https://doi.org/10.35090/gatech/81252)。发布说明列出 `manually_checked_TA_label/obset.xlsx`、`valset1.csv` 与 `rubric_outputs/P*.json`；仓库总README对test文件名称的写法不同，导入时应以实际包清单核对。人工纠正转写只覆盖部分样本，不能把所有模型转写统一标为人工真值。教材题干版权限制独立于代码许可。

DrawEduMath 的标签来源、字段与使用条件见[官方数据卡](https://huggingface.co/datasets/Heffernan-WPI-Lab/DrawEduMath/blob/main/README.md)。教师描述/教师QA与GPT-4o/Claude生成QA必须分开；即使QA判断答案正误，也不等于教师分项分数。

MathWriting 的数据/代码许可区分见[官方README](https://github.com/google-research/google-research/blob/master/mathwriting/README.md)。CROHME的模态与标注范围见[作者发布记录](https://zenodo.org/records/8428035)。不能用这两类转写标签推算不存在的过程分。

微积分研究的实际评分比较与未来发布计划见[论文第6、8节](https://arxiv.org/html/2603.00895v1)。Mendeley候选的人工评分文件及许可见[原始发布页](https://data.mendeley.com/datasets/sf3kvjwknt/1)；其匿名化说明是发布者声明，本任务未做图像隐私复核。

后续若决定采用真实评分数据，先补实际文件清单/哈希、题目与满分对齐、原始rubric、人工标签列及标注者来源、图像脱敏检查和许可快照，再做题目/学生分组的固定切分。每题最多先抽少量可核对样本；原始转写、人工修正和评分结果分别保存。无完整题干或分项标签的样本只用于其实际可支持的指标，不填造缺失分数。
