# OCR Benchmark 工具

## OmniDocBench manifest 生成器

脚本：

`tools/ocr_benchmark/prepare_omnidocbench.py`

它只读取和校验本地数据，不调用 OCR 或其他模型，不产生 API 费用。

### 首次完整校验

```bash
python \
  tools/ocr_benchmark/prepare_omnidocbench.py \
  --dataset-root data/benchmarks/OmniDocBench \
  --output-dir artifacts/ocr_benchmark/omnidocbench_manifest \
  --seed 20260723
```

首次运行建议保留默认的 PyMuPDF 实际解码检查。

### 重复快速生成

已经完成首次解码验收后，可以跳过完整解码；文件头、真实尺寸、标注尺寸、SHA-256 和抽样校验仍会执行：

```bash
python \
  tools/ocr_benchmark/prepare_omnidocbench.py \
  --dataset-root data/benchmarks/OmniDocBench \
  --output-dir artifacts/ocr_benchmark/omnidocbench_manifest \
  --seed 20260723 \
  --skip-decode-check
```

### 输出

- `manifest.jsonl`：全部 1651 页；
- `smoke.jsonl`：固定 12 页；
- `pilot.jsonl`：固定 120 页，包含全部 Smoke 页面；
- `summary.json`：数据完整性、分布、配额及不变量。

输出目录由 Git 忽略。

### 核心字段

- `sample_id`：稳定、唯一的样本 ID；
- `image_path`、`image_sha256`、`image_bytes`；
- `image_width`、`image_height`、`image_format`、`image_suffix`；
- `extension_content_mismatch`：扩展名与实际图片格式是否不一致；
- `over_product_limit`：是否超过当前 10 MiB 单图限制；
- `annotation_block_count`、`active_annotation_block_count`、`content_annotation_block_count`；
- `selection_eligible`：是否至少有一个非页眉、页脚、页码或废弃区的有效内容块；
- `data_source`、`language`、`layout`、`special_issues`、`subset`；
- `purpose`：`problems`、`submissions` 或 `reference`；
- `sampling_group`：互斥抽样组；
- `splits`：所属的 `smoke`、`pilot` 集合。

### 抽样规则

- 固定 seed：`20260723`；
- 困难子集优先于模糊扫描，模糊扫描优先于试卷/笔记来源；
- 每个抽样组内按语言做确定性平衡；
- Smoke 配额为 4 试卷、3 笔记、2 模糊扫描、1 公式困难、1 表格困难、1 布局困难；
- Pilot 配额为 48 试卷、36 笔记、12 模糊扫描，以及公式/表格/布局困难各 8；
- Smoke 是 Pilot 的严格子集；
- 只有 `selection_eligible=true` 的页面可以进入 Smoke/Pilot，边缘标注-only 空白页仅保留在全量 manifest；
- 数据存在超限候选时，Smoke 至少保留 1 张超限图片。

### 测试

```bash
python \
  -m pytest -q backend/tests/test_prepare_omnidocbench.py
```

测试覆盖：

- 相同 seed 的逐字节确定性；
- Smoke/Pilot 数量、配额、包含关系和超限样本；
- purpose 映射和特殊标记规范化；
- 图片缺失、尺寸不一致、路径穿越和候选不足；
- 扩展名与真实图片格式不一致的诊断。
- 仅页眉/页脚/页码页面不进入 Smoke/Pilot。

## 已批准 Smoke 的 dry-run

批准配置：

`tools/ocr_benchmark/config/omnidocbench_smoke_20260723.approved.json`

执行：

```bash
python \
  tools/ocr_benchmark/run_omnidocbench.py
```

不带 `--execute` 时，执行器只生成 dry-run 计划，不调用 OCR 模型。它会校验：

- 配置已经批准，样本顺序、配额和 Pilot 包含关系不变；
- 数据集 revision、标注 SHA-256、图片大小及图片 SHA-256；
- 当前产品的 10 MiB 输入限制；
- 按文件后缀声明的 MIME 与实际图片格式；
- 每页的 OCR purpose、prompt SHA-256、缓存键和安全输出路径。

默认产物：

`artifacts/ocr_benchmark/omnidocbench_smoke_20260723/dry_run_plan.json`

### 运行已批准 Smoke

真实模型调用必须显式增加 `--execute`：

```bash
python \
  tools/ocr_benchmark/run_omnidocbench.py \
  --execute
```

执行器会：

- 复用现有 `extract_text_from_upload` 和 `LLMVisionOCRSkill`；
- 默认并发 2，单页可重试 3 次；
- 每页完成后原子写入 `results.jsonl`，成功页可断点续跑；
- 以图片哈希、provider/model、purpose、prompt 和请求参数生成缓存键；
- 对超限原图实际验证 413，并以固定 PyMuPDF 参数生成独立的 JPEG 规范化轨；
- 将预测、缓存、规范化输入和运行汇总写入 Git 忽略的 artifacts 目录。

失败样本不会在后续命令中自动重复计费。确认需要重跑失败页时使用：

```bash
python \
  tools/ocr_benchmark/run_omnidocbench.py \
  --execute \
  --retry-failures
```
