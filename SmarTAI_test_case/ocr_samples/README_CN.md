# OCR 作答上传回归样本

## 可直接上传的本地样本

- 作答图片：`S003_synthetic_geography_handwriting.png`
- 配套题目：`../../manual_test_samples/omnidocbench/case_03_geography_questions.md`
- 图片规格：PNG，1024 × 1536，RGB
- SHA-256：`6cc84c3f62a37faae4399ab37016ec2a5473a6eac7afe81a885489fb52f765e3`

这是 2026-07-30 生成的**合成回归样本**，不含真实学生资料，也不是
OmniDocBench 原图。图片内写有“合成测试样本”，可用于日常前端上传、
OCR、题号映射和评分链路检查。

预期识别内容：

```text
S003 合成测试样本
1. 图例表示地理事物的各种符号；注记是说明地理事物名称的文字，或说明数量的数字。
2. 海拔是某地点高出海平面的垂直距离；相对高度是某地点高出另一地点的垂直距离。
3. 等高线稀疏表示坡缓，等高线密集表示坡陡。
4. 平原、高原、山地、丘陵、盆地。
```

## 最短手测步骤

1. 用配套 Markdown 创建一个独立任务并完成题目确认。
2. 在“上传学生作答”选择本目录 PNG；使用“按文件名识别学号”。
3. 确认进度页出现 OCR 步骤，并最终进入作答复核。
4. 在复核页检查 `S003`、四个题号、负号/标点和五种地形是否完整。
5. Raw 轨不改 OCR 文本直接评分；另建相同任务做 Corrected 轨，修正 OCR
   后再评分。不要在同一任务评分后再修改作答。

## xts 的原始固定样本

xts 的 12 页 OmniDocBench smoke 固定清单中，第 7 页也是中文地理手写
笔记，且用途明确为 `submissions`：

- 原图：`data/benchmarks/OmniDocBench/images/notes_1ba14cb325bc448f7201b20502ecf2b5_10.jpg`
- SHA-256：`377125b604f4be962c51142429bb57b8854607cc3dd3b49c0ff48e99acf4eaac`
- 固定清单：`../../tools/ocr_benchmark/config/omnidocbench_smoke_20260723.approved.json`
- 运行报告：`../../tools/ocr_benchmark/OMNIDOCBENCH_SMOKE_RUN_REPORT_CN.md`

该次 smoke 中第 7 页得到 580 个字符、`[unclear]` 为 0、成功响应耗时
19.40 秒。这个结果证明链路和非空输出，不等于已经完成正式 OCR
准确率评测。原始数据受 OmniDocBench 的研究/非商业许可约束，应保持
本地并由 Git 忽略。

运行 OCR 图片或扫描版 PDF 前，至少启用一个支持图片输入的 Gemini、
OpenAI 或 Anthropic 模型。
