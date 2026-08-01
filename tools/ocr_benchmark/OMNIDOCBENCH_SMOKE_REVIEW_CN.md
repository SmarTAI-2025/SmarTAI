# OmniDocBench OCR Smoke 12 页确认单

- 状态：**已确认并冻结；Smoke 已运行，运行链路通过**
- 确认日期：2026-07-24
- dry-run 日期：2026-07-24
- Smoke 运行日期：2026-07-24
- 数据快照：`aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec`
- 标注 SHA-256：`a45cd84b04ad8b793e775089640e6b681209abea33ead54c1828ddca35fae496`
- 抽样 seed：`20260723`

## 覆盖结论

- 配额：试卷 4、笔记 3、模糊扫描 2、公式困难 1、表格困难 1、布局困难 1；
- 语言：英文 3、简体中文 4、中英混排 4、繁体中文 1；
- 实际编码：PNG 6、JPEG 6；
- 12 页均有有效内容标注，且均属于固定的 120 页 Pilot；
- 包含 1 张 12.58 MiB 超限页，用来验证“原始轨拒绝 + 规范化轨继续处理”；
- 包含 1 张扩展名为 `.png`、实际内容为 JPEG 的页，用来验证格式探测；
- 已逐页人工查看，不含个人敏感信息；仅按 OmniDocBench 的研究、非商业许可用于本地测试。

## 已确认的 12 页

| # | 抽样组 / purpose | 语言与布局 | 大小 | 页面内容 | 本页主要验证点 |
|---:|---|---|---:|---|---|
| 1 | 模糊扫描 / problems | 英文，单栏 | 4.69 MiB | [高考英语听力材料与题目](../../data/benchmarks/OmniDocBench/images/exam_paper_2004-2019上海高考英语听力原文和答案_page_050.png) | 严重透印、模糊扫描、长段英文 |
| 2 | 试卷 / problems | 中英混排，单栏 | 0.42 MiB | [中考英语解析与机器人阅读表格](../../data/benchmarks/OmniDocBench/images/exam_paper_2016年安徽省中考英语试题（解析）_page_012.png) | 中英混排、横向表格、表格内图片 |
| 3 | 试卷 / problems | 中英混排，单栏 | 0.24 MiB | [中考英语空白试卷](../../data/benchmarks/OmniDocBench/images/exam_paper_2018年广西北海、钦州、南宁、来宾、崇左、防城港、北部湾经济区中考英语试题（空白卷）_page_001.png) | 清晰印刷体基线、题号与选项 |
| 4 | 试卷 / problems | 英文，多栏 | 0.35 MiB | [2007 Putnam 数学竞赛正式试题](../../data/benchmarks/OmniDocBench/images/exam_paper_en-file-putnam-archive_2007_Problems_2007_page_001.png) | 双栏阅读顺序、密集数学公式与题号 |
| 5 | 模糊扫描 / problems | 简中，双栏 | 0.09 MiB | [小学数学答案材料双页扫描](../../data/benchmarks/OmniDocBench/images/jiaocaineedrop_jiaocai_needrop_en_2360.jpg) | 低清小字、双页、表格与红色答案 |
| 6 | 试卷 / problems | 简中，复杂布局 | 3.01 MiB | [中文阅读与选择题试卷](../../data/benchmarks/OmniDocBench/images/jiaocaineedrop_jiaocai_needrop_en_237.jpg) | 水印、多栏题目、题号与选项 |
| 7 | 笔记 / submissions | 简中，单栏 | 0.27 MiB | [中文地理手写笔记](../../data/benchmarks/OmniDocBench/images/notes_1ba14cb325bc448f7201b20502ecf2b5_10.jpg) | 密集中文手写、符号与要点 |
| 8 | 笔记 / submissions | 中英混排，单栏 | 0.34 MiB | [英语语法中英手写笔记](../../data/benchmarks/OmniDocBench/images/notes_f7f010b78016aeebd76e56d9283eb67f_46.jpg) | 英文例句、中文解释、手写混排 |
| 9 | 笔记 / submissions | 中英混排，单栏 | 0.32 MiB | [英语阅读推断手写笔记](../../data/benchmarks/OmniDocBench/images/notes_f7f010b78016aeebd76e56d9283eb67f_94.jpg) | 手写段落、英文选项、中文推理过程 |
| 10 | 布局困难 / problems | 简中，复杂布局 | 12.58 MiB | [变形的英语教材双页](../../data/benchmarks/OmniDocBench/images/page-4540d82a-19d8-44ef-8f18-6d42fdb2a2de.png) | 几何变形、双页阅读顺序、超限输入 |
| 11 | 公式困难 / reference | 英文，单栏 | 0.49 MiB | [微分方程教材公式页](../../data/benchmarks/OmniDocBench/images/page-8c21f73a-b849-4f71-9573-57d50fbcc180.png) | 行内/独立公式；`.png` 后缀但实际为 JPEG |
| 12 | 表格困难 / reference | 繁中，单栏 | 0.48 MiB | [繁体中文财报表格页](../../data/benchmarks/OmniDocBench/images/page-b9d507e7-1239-42de-b70a-d7ca65393dc9.png) | 跨列表头、括号负数、繁体正文 |

## 本次人工整理

1. 自动候选中的 `notes_f7f010b78016aeebd76e56d9283eb67f_5` 只有 `NO./Date` 页眉，已剔除。生成器 schema v2 会保留它在全量 manifest 中，但禁止它进入 Smoke/Pilot。
2. 自动候选中的 2005 Putnam 解答页已替换为第 4 页的 2007 Putnam 正式试题。替补页原本就在固定 Pilot 中，因此仍满足 Smoke 是 Pilot 子集。

## 确认后的建议执行顺序

1. 已将固定配置标记为 `approved`；以后不因结果好坏换样；
2. 已完成执行器 dry-run：配置、哈希、purpose、缓存键和输出路径校验均通过，模型调用数为 0；
3. 已完成原始产品轨：11 张 OCR 成功；第 10 张按当前 10 MiB 限制得到预期输入拒绝；
4. 已完成第 10 张的固定参数规范化分析轨，OCR 成功；
5. 已核对非空输出、错页、重试、时延、格式探测和产物完整性；
6. 完成正式 OCR 精度评测，并对试卷/笔记页追加题目解析、作答解析和后台评分观测；
7. 提交 Smoke 报告，通过后再申请运行 120 页 Pilot。

固定配置：

`tools/ocr_benchmark/config/omnidocbench_smoke_20260723.approved.json`

冻结依据：用户于 2026-07-24 回复“确认这 12 页”。

dry-run 结果：11 页计划进入原始 OCR，1 页计划得到预期输入拒绝；共生成 11 个唯一缓存键。

Smoke 运行结果：12 个 OCR 输出成功，1 个超限原图得到预期拒绝，无失败或空输出。详见：

`tools/ocr_benchmark/OMNIDOCBENCH_SMOKE_RUN_REPORT_CN.md`
