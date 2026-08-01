# 6.1(A) 图片与扫描件识别接入方案

本文档对应 `PROJECT_STATUS_AND_ROADMAP_CN(1).md` 中 6.1(A) 的任务：为 SmarTAI 增加图片、扫描 PDF、手写数理作答的视觉/OCR 识别入口，并把识别结果接回现有题目抽取、学生答案解析、人工订正和评分流程。

## 1. 背景与现状

当前工程已经具备完整的任务流：

1. 创建任务。
2. 上传题目文件。
3. 从题目文本中抽取题号、题干、评分标准。
4. 上传学生答案文件或压缩包。
5. 从学生答案文本中解析每个学生、每道题的作答。
6. 进入人工订正。
7. 执行多专家评分与结果分析。

但当前文件解析链路基本假设输入已经是可直接读取的文本：

- `backend/tools/file_processing.py`
  - `decode_text_bytes` 负责文本解码。
  - `extract_text_from_pdf` 使用 PyMuPDF 的 `page.get_text()` 抽取 PDF 文本。
  - `extract_files_from_archive` 对压缩包内文件基本按文本处理。
- `backend/api/tasks.py`
  - 题目上传时，PDF 走 `extract_text_from_pdf`，其他文件走 `decode_text_bytes`。
  - 学生答案上传时，压缩包走 `extract_files_from_archive`。
  - 参考答案、测试点上传也只按文本/PDF 处理。
- `backend/agents/ingest_agent.py`
  - `extract_problems(text, ...)` 接收纯文本。
  - `parse_student_answers(files_data, ...)` 接收 `{"filename": ..., "content": ...}` 结构。
- 前端上传页面目前主要提示 `.txt/.pdf/.docx` 或压缩包，不明确支持图片和扫描 PDF。

因此，6.1(A) 的关键不是重写抽题、解析或评分逻辑，而是在现有 ingest 之前补一层“文件转文本”的能力：普通文本仍走原路径；图片和扫描 PDF 先走视觉/OCR，再把结果作为文本交给现有链路。

## 2. 目标

第一阶段目标：

1. 支持题目文件上传图片：`.jpg/.jpeg/.png/.webp`。
2. 支持题目上传扫描 PDF。
3. 支持学生答案上传图片、扫描 PDF，以及包含这些文件的压缩包。
4. OCR/视觉识别输出统一转成带 LaTeX 的纯文本或 Markdown。
5. 识别结果继续进入现有题目抽取、学生答案解析和人工订正流程。
6. 默认使用多模态大模型视觉能力，不在第一阶段强依赖 Mathpix。
7. 为后续 Mathpix 或其他 OCR 服务预留抽象接口。

非目标：

1. 第一阶段不重做评分链路。
2. 第一阶段不重做人工订正页面，只做必要的提示或标记。
3. 第一阶段不把 Mathpix 做成默认路径。
4. 第一阶段不承诺解决所有低质量扫描件，只保证可识别、可订正、可追踪。
5. 第一阶段不强行补齐 `.docx/.ipynb` 的真实解析能力，除非另开任务。

## 3. 推荐架构

新增或调整后的主链路如下：

```text
上传文件
  -> 统一文件识别入口
      -> 普通 txt：文本解码
      -> 普通 PDF：PyMuPDF 文本抽取
      -> 扫描 PDF：页面渲染为图片，再走视觉 OCR
      -> 图片：视觉 OCR
      -> 压缩包：逐文件走同一套逻辑
  -> 得到纯文本
  -> extract_problems / parse_student_answers
  -> 人工订正
  -> 评分
```

核心原则：

- 把 OCR 影响限制在文件处理层和 LLM provider 层。
- 不让 `ingest_agent.py` 感知“这是 OCR 文本还是原生文本”。
- 不让任务 API 里散落大量文件类型判断。
- 保持人工订正作为 OCR 误差的兜底机制。

### 3.1 Skill 定位修正

OCR 应作为“摄入阶段 skill”实现，而不是接入现有 `backend/skills/base.py`
里的 `GradingSkill` 注册体系。现有 `GradingSkill` 是按题型进行评分的配方，
会被 `grading_agent.py` 和多专家评分流程调用；OCR 发生在 `extract_problems`
和 `parse_student_answers` 之前，职责是把文件转成可订正的文本。

因此实现时建议新增独立的 `OCRIngestSkill`：

- 目标文件：`backend/skills/ocr_ingest.py`。
- 不使用 `@register_skill`，不继承 `GradingSkill`。
- 接收一个支持 vision 的 LLM provider，或后续接入的 Mathpix provider。
- 对外暴露 `recognize_images(...) -> OCRResult`。
- `backend/tools/file_processing.py` 只依赖这个 skill 的小接口，不直接关心
  OpenAI/Gemini/Anthropic 的图片消息格式。

## 4. 后端改造方案

### 4.1 文件处理层

目标文件：

- `backend/tools/file_processing.py`

建议新增统一入口：

```python
async def extract_text_from_upload(
    file_bytes: bytes,
    filename: str,
    ocr_skill: OCRIngestSkill | None = None,
    purpose: Literal["problems", "submissions", "reference", "test_cases"] = "submissions",
    reporter: ProgressReporter | None = None,
) -> str:
    ...
```

该函数负责把单个文件转换为文本。

建议支持规则：

| 文件类型 | 处理方式 |
| --- | --- |
| `.txt/.md/.csv` | 继续使用 `decode_text_bytes` |
| 普通 `.pdf` | 继续使用 `extract_text_from_pdf` |
| 扫描 `.pdf` | PyMuPDF 抽不到有效文本时，渲染页面为图片并走 OCR |
| `.jpg/.jpeg/.png/.webp` | 直接走 OCR |
| 其他类型 | 第一阶段明确报不支持，不静默丢弃 |

扫描 PDF 判定建议：

```text
抽取文本后，去掉空白字符。
如果字符数 < 50，或平均每页字符数 < 30，则判定为疑似扫描 PDF。
```

这个阈值要保守，避免把正常 PDF 误送到视觉模型，造成额外成本和延迟。

PDF 渲染建议：

- 使用现有 PyMuPDF 依赖。
- 渲染比例建议 2x，即 `fitz.Matrix(2, 2)`。
- 输出 PNG bytes。
- 每页 OCR 后拼接成文本，并加页码标记：

```text
[page 1]
...

[page 2]
...
```

压缩包处理建议增强 `extract_files_from_archive`：

```python
async def extract_files_from_archive(
    file_bytes: bytes,
    filename: str,
    ocr_skill: OCRIngestSkill | None = None,
    purpose: Literal["submissions", "problems"] = "submissions",
    reporter: ProgressReporter | None = None,
) -> list[dict[str, str]]:
    ...
```

输出结构继续保持：

```python
[
    {
        "filename": "student_001/page1.jpg",
        "content": "OCR 后文本..."
    }
]
```

这样 `parse_student_answers(files_data, ...)` 不需要大改。

### 4.2 OCRIngestSkill 抽象

建议新增文件：

- `backend/skills/ocr_ingest.py`

接口示例：

```python
from dataclasses import dataclass
from typing import Literal, Protocol

@dataclass
class OCRImage:
    data: bytes
    media_type: str
    label: str | None = None

@dataclass
class OCRResult:
    text: str
    provider: str
    warnings: list[str]

class OCRIngestSkill(Protocol):
    async def recognize_images(
        self,
        images: list[OCRImage],
        purpose: Literal["problems", "submissions", "reference", "test_cases"],
    ) -> OCRResult:
        ...
```

第一阶段实现：

- `LLMVisionOCRSkill`
  - 使用当前配置中的多模态 LLM provider。
  - 不额外引入第三方 OCR 服务。
- `MathpixOCRSkill`
  - 只保留接口、配置结构和 TODO。
  - 没有 API key 时不可用。

建议不要把 Mathpix 作为 `backend/llm/providers.py` 里的普通 LLM provider。Mathpix 是 OCR 服务，不是通用对话模型，应该放在 OCR 抽象后面。

### 4.3 OCR Prompt 设计

题目识别 prompt：

```text
你是一个数理题目 OCR 转写器。请把图片中的题目内容转写为纯文本 Markdown。

要求：
1. 保留题号、题干、选项、已给条件、评分标准、附图说明。
2. 数学公式使用 LaTeX。
3. 不要解题，不要补充图片中没有的信息。
4. 无法辨认的内容标记为 [unclear]。
5. 如果有多页，按页面顺序输出。
```

学生答案识别 prompt：

```text
你是一个手写数理作答 OCR 转写器。请把图片中的学生作答转写为纯文本 Markdown。

要求：
1. 保留学生姓名、学号、班级等身份信息。
2. 保留题号和每道题的作答步骤。
3. 数学公式使用 LaTeX。
4. 不要批改，不要推断学生未写出的步骤。
5. 看不清的字、公式或数字标记为 [unclear]。
6. 尽量保留划改、箭头、补充说明等作答痕迹。
```

参考答案和测试点识别 prompt 可以更接近题目识别，但强调“不要生成新答案，只转写原文”。

### 4.4 LLM Provider 多模态接口

目标文件：

- `backend/llm/providers.py`

建议在 `BaseProvider` 增加：

```python
supports_vision: bool = False

async def ainvoke_vision(
    self,
    prompt: str,
    images: list[VisionImage],
) -> LLMResponse:
    raise NotImplementedError
```

其中 `VisionImage` 可以定义为：

```python
@dataclass
class VisionImage:
    data: bytes
    media_type: str
    filename: str | None = None
```

各 provider 实现策略：

| Provider | 第一阶段策略 |
| --- | --- |
| Gemini | 实现视觉调用 |
| OpenAI | 实现视觉调用 |
| Anthropic | 实现视觉调用 |
| Zhipu | 暂时标记不支持，除非确认当前模型和 LangChain 适配格式 |

provider 选择逻辑：

1. 如果当前任务选择的 provider 支持 vision，直接使用。
2. 如果不支持，尝试从已配置 provider 中选择第一个支持 vision 的 provider。
3. 如果没有可用视觉 provider，任务返回明确错误：当前未配置可识别图片/扫描 PDF 的视觉模型。

建议在 `ExpertRegistry` 增加 `pick_vision(preferred: BaseProvider | None = None)`，
由 API 层构造 `LLMVisionOCRSkill(vision_provider)` 后传给文件处理层。

注意事项：

- 不同 provider 的图片 content block 格式不同，差异必须封装在 provider 内部。
- `file_processing.py` 和 `ocr.py` 不应关心 OpenAI/Gemini/Anthropic 的具体消息格式。
- 要避免把 base64 拼接逻辑散落到业务 API。

### 4.5 任务 API 接入

目标文件：

- `backend/api/tasks.py`

需要调整的入口：

1. `task_extract_problems`
   - 当前直接按 PDF/text 分支。
   - 改为调用 `extract_text_from_upload(..., purpose="problems")`。

2. `task_parse_submissions`
   - 当前调用旧版 `extract_files_from_archive`。
   - 改为调用增强版 `extract_files_from_archive(..., purpose="submissions")`。

3. `_read_text_for_parse`
   - 参考答案、测试点上传也应复用同一套文件转文本逻辑。
   - 这样扫描版参考答案或测试点说明也能被识别。

4. 旧的 `backend/api/ingest.py`
   - 这是偏旧的 preview/ingest 路由。
   - 建议同步改成复用同一 helper，避免两个入口行为不一致。

保留不变的部分：

- `extract_problems(text, provider, problem_store, reporter)`
- `parse_student_answers(files_data, problems_data, student_store, provider, reporter)`
- 题目人工订正接口。
- 学生答案人工订正接口。
- 评分接口。

### 4.6 状态与错误信息

建议在任务进度 reporter 中加入更明确的消息：

```text
正在读取文件...
检测到扫描 PDF，开始按页识别...
正在识别第 1/5 页...
OCR 识别完成，进入题目结构化抽取...
```

常见错误应明确区分：

- 文件类型不支持。
- 当前 provider 不支持图片识别。
- 未配置任何视觉模型。
- PDF 页数超过限制。
- 图片过大。
- OCR 调用失败。

不要把 OCR 失败伪装成“没有解析到题目”或“学生答案为空”，否则后续排查成本很高。

## 5. 前端改造方案

目标文件：

- `frontend/app/src/routes/tasks/TaskUploadPage.tsx`
- `frontend/app/src/api/tasks.ts`（通常无需改接口，只需保持 multipart 上传）
- `frontend/app/src/types/task.ts`（仅当要展示 OCR 元信息时再改）

旧 Reflex 前端仍可后续同步，但当前 README 指定的新教师端入口是
`frontend/app/`，第一阶段应优先改 React 上传页。

### 5.1 题目上传页

需要调整：

- 上传说明从 `.txt / .pdf / .docx` 改成 `.txt / .pdf / .jpg / .png / .webp`。
- 明确说明 PDF 可以是扫描件。
- `accept` 增加：
  - `text/plain`
  - `application/pdf`
  - `image/jpeg`
  - `image/png`
  - `image/webp`

建议文案：

```text
支持 .txt、.pdf（含扫描件）、.jpg、.png、.webp。扫描件和图片会调用视觉模型识别，耗时会更长。
```

### 5.2 学生答案上传页

需要调整：

- 文案说明压缩包内可包含图片和扫描 PDF。
- 如果后端支持单文件答案，也可以允许用户直接上传单张图片或单个 PDF。

建议文案：

```text
支持上传压缩包，包内可包含 .txt、.pdf（含扫描件）、.jpg、.png、.webp。也支持单个答案文件上传。
```

### 5.3 人工订正页面

第一阶段不需要重做页面。

可选增强：

- 对 OCR 输出中的 `[unclear]` 做高亮。
- 如果某份答案来自图片/扫描 PDF，在答案详情里显示“来源：OCR 识别”。
- 如果 OCR provider 返回 warning，在学生答案旁边显示轻量提示。

这些是体验优化，不是 6.1(A) 的第一优先级。

## 6. 配置与限制

建议新增配置项，放在现有 settings/config 体系中：

```text
SMARTAI_OCR_DEFAULT_PROVIDER=llm_vision
SMARTAI_OCR_MAX_PDF_PAGES=30
SMARTAI_OCR_MAX_IMAGE_BYTES=10485760
SMARTAI_OCR_RENDER_DPI_SCALE=2
SMARTAI_OCR_CONCURRENCY=2
SMARTAI_OCR_TEXT_MIN_CHARS=50
SMARTAI_MATHPIX_APP_ID=
SMARTAI_MATHPIX_APP_KEY=
```

说明：`backend/config.py` 使用 `env_prefix="SMARTAI_"`，文档和 `.env`
示例应使用带 `SMARTAI_` 前缀的环境变量名。

默认策略：

- 默认 OCR provider 使用 `llm_vision`。
- 没有配置 Mathpix 时不启用 Mathpix。
- 页数、大小、并发限制必须默认开启。

为什么需要限制：

- 视觉模型调用成本明显高于纯文本调用。
- 扫描 PDF 可能几十页甚至上百页。
- 如果没有限制，一个班级的答题卡压缩包可能拖垮任务队列。

## 7. 数据结构建议

第一阶段可以不改数据库模型，只在内存处理过程中保留 OCR 元信息。

如后续希望前端展示 OCR 来源，可在 `files_data` 内部扩展：

```python
{
    "filename": "student_001/page1.jpg",
    "content": "...",
    "source_type": "image",
    "ocr_provider": "openai",
    "warnings": ["..."]
}
```

但要注意：

- `parse_student_answers` 当前只依赖 `filename` 和 `content`。
- 新字段必须兼容旧逻辑。
- 不要为了第一阶段展示元信息而大改存储结构。

## 8. 测试方案

### 8.1 单元测试

建议新增或扩展 `backend/tests` 中的文件处理测试。

覆盖用例：

1. `.txt` 文件仍直接解码。
2. 普通 PDF 抽到足够文本时不调用 OCR。
3. 空文本 PDF 或扫描 PDF 会触发 OCR mock。
4. `.jpg/.png/.webp` 会触发 OCR mock。
5. 压缩包中混合 `.txt/.pdf/.png` 时能返回多个 `files_data`。
6. 不支持的文件类型返回清楚错误。
7. 文件过大、页数过多时返回清楚错误。

### 8.2 Provider 测试

不建议在自动测试中真实调用外部模型。

应使用 fake provider 验证：

- `supports_vision` 为 false 时走错误分支。
- `supports_vision` 为 true 时会收到正确的 prompt 和图片列表。
- OCR 输出会被正确拼接回文本。

### 8.3 API 测试

覆盖：

1. 上传图片题目文件，mock OCR 后进入题目抽取。
2. 上传扫描 PDF，mock OCR 后进入题目抽取。
3. 上传包含图片答案的压缩包，mock OCR 后进入学生答案解析。
4. provider 不支持 vision 时，API 返回明确错误。

### 8.4 前端检查

至少执行：

- React `npm run typecheck`。
- 手工打开题目上传页，确认可选择图片。
- 手工打开学生答案上传页，确认文案和上传限制一致。

### 8.5 手工端到端验证

建议准备一套最小测试样例：

1. 一张题目图片，包含 2 道数学题。
2. 一份扫描 PDF 题目。
3. 一个学生答案图片压缩包。
4. 一张低质量手写图片，验证 `[unclear]` 和人工订正流程。

验证流程：

1. 创建任务。
2. 上传题目图片。
3. 检查题目抽取结果。
4. 人工订正题目。
5. 上传学生答案图片压缩包。
6. 检查学生答案解析结果。
7. 人工订正答案。
8. 执行评分。

## 9. 实施顺序

建议分 5 个小 PR 或 5 个阶段做，便于验证和回滚。

### 阶段 1：抽象和文件转文本

改动：

- 新增 `OCRIngestSkill` 抽象。
- 新增 fake/mock OCR provider。
- 增强 `file_processing.py`。
- 支持图片、扫描 PDF、压缩包内图片。

当前状态：

- 尚未实现。当前代码仍只有 `decode_text_bytes`、`extract_text_from_pdf`
  和旧版 `extract_files_from_archive`；图片会被忽略或解码失败。
- 当前 `BaseProvider` 没有 `supports_vision` / `ainvoke_vision`。
- 当前 `ExpertRegistry` 没有视觉 provider 选择逻辑。
- 当前 React 上传页仍提示图片/OCR 是“后续接入项”。

验收：

- 不接真实模型，只用 fake OCR 也能跑通文件转文本测试。

### 阶段 2：接入任务 API

改动：

- `task_extract_problems` 改用统一文件识别入口。
- `task_parse_submissions` 改用增强版压缩包解析。
- `_read_text_for_parse` 复用同一 helper。
- 旧 `ingest.py` 同步接入或明确降级。

验收：

- mock OCR 后，现有题目抽取和学生答案解析仍能跑通。

### 阶段 3：实现真实视觉 provider

改动：

- `BaseProvider` 增加 `supports_vision` 和 `ainvoke_vision`。
- Gemini/OpenAI/Anthropic 分别实现。
- Zhipu 暂不支持或单独确认。
- 加 provider 选择和错误提示。

验收：

- 代码级验收：provider 能构造标准图片消息，Gemini/OpenAI/Anthropic 的 `ainvoke_vision` 可被 fake client 调用，文本 provider 不声明 vision 能力。
- 真实效果验收：至少配置一个视觉模型 API key，并提供 1-2 张题目图片或手写答案图片，确认 OCR 输出能进入后续抽题/解析链路。

### 阶段 4：前端上传入口

改动：

- 题目上传页放开图片类型。
- 学生答案上传页更新文案和 accept。
- 可选高亮 `[unclear]`。

验收：

- 用户能从 UI 选择图片/扫描 PDF。
- 上传后任务进度能显示 OCR 阶段。

### 阶段 5：限流、错误处理和文档

改动：

- 增加页数、大小、并发限制。
- 补测试。
- 更新 README 或项目路线说明。

验收：

- 大文件、无视觉 provider、OCR 失败都有明确提示。

## 10. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| OCR 成本高 | 扫描 PDF 或大量图片会消耗较多 token/费用 | 加页数、大小、并发限制；普通 PDF 优先文本抽取 |
| 识别质量不稳定 | 题号、公式、学生步骤可能出错 | 保留人工订正；prompt 要求 `[unclear]`；可选高亮 |
| provider 图片格式差异大 | OpenAI/Gemini/Anthropic 接入容易写散 | 统一封装在 `ainvoke_vision` 内 |
| 当前默认 provider 不支持视觉 | 图片上传无法处理 | 增加 provider 选择逻辑和明确错误 |
| 压缩包内文件复杂 | 目录、隐藏文件、非法文件名、超大文件 | 做文件过滤、路径清洗、大小限制 |
| 扫描 PDF 判定误差 | 普通 PDF 被误判会增加成本 | 阈值保守；先文本抽取，只有低文本量才 OCR |
| Mathpix 集成过早 | 增加密钥、成本和外部依赖复杂度 | 第一阶段只留抽象，默认 LLM vision |

## 11. 推荐的最小可交付版本

最小可交付版本应包含：

1. 题目上传支持图片和扫描 PDF。
2. 学生答案上传支持图片和扫描 PDF。
3. 压缩包内图片/PDF 能被逐个识别。
4. OCR 输出进入现有人工订正页面。
5. 至少一个视觉模型 provider 可用。
6. 没有视觉模型时给出明确错误。
7. 有基础页数、大小、并发限制。
8. 有文件处理和 API 层测试。

可以暂缓：

1. Mathpix 真实接入。
2. OCR 置信度展示。
3. `[unclear]` 高亮。
4. 复杂版 OCR 结果存储。
5. `.docx/.ipynb` 的真实解析能力。

## 12. 结论

6.1(A) 最稳妥的落地方式是“文件转文本层扩展 + provider 多模态能力 + 前端放开上传类型”。不要把 OCR 逻辑深入到题目抽取、答案解析或评分 agent 里。这样既能快速支持图片和扫描 PDF，又能最大限度复用现有人工订正和评分流程。

第一阶段建议默认走 LLM vision，Mathpix 只留接口。等图片/扫描 PDF 的端到端流程稳定后，再根据识别准确率和成本决定是否接入 Mathpix 作为高精度 OCR provider。
