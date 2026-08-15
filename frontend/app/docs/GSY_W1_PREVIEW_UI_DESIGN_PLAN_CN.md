# 原文件左右对照 UI 设计与实施计划（GSY-W1-PREVIEW-UI）

> 周期：Week 1（2026-08-03～08-09）
> 主责：gsy；本文件的完成范围只包含 Week 1 mock UI，lyj 只确认文件状态合同。
> 后续：真实鉴权读取属于独立的 Week 2 `GSY-W2-PREVIEW-AND-REGISTRATION`，不计入本任务完成定义。
> 单一目标：教师不离开当前复核页，就能把整份 PDF/图片原稿与可编辑识别内容放在一起核对。

## 1. 范围

### Week 1 必做

- 学生作答复核页：打开/关闭整份学生原文件。
- 题目资料审核页：打开/关闭整份题目原文件。
- PDF、图片、加载、读取失败、处理中、不可用、任务完成后已清理七类状态。
- 桌面默认原稿与识别内容各占 50%，教师可拖动中间分隔条调整比例。
- 使用显式本地 mock 演示，不等待 lyj 的数据库和文件接口。
- 保持当前搜索、题目导航、编辑、保存、未保存拦截和键盘导航行为。

### 不做

- 不自动定位页码、题目、行或 OCR 坐标。
- 不支持 Word、HTML、SVG；不新增下载、永久链接或“新窗口打开”。
- 不做文件缩放工具条、批注、旋转或页缩略图。
- 不修改 `backend/db/`、`backend/storage/`、OCR/批改 Skill 或 owner 授权。

## 2. 设计方向

沿用现有 Figma 产品壳，不创造第二套视觉。唯一新增识别点是“原稿工作台”：预览区以浅灰画布承托白色文件，预览区与识别内容之间有一条 2px 主色“对照脊线”。脊线同时是可拖动分隔条的视觉中心，表达“左边证据、右边当前确认版”，不是额外装饰。

自检：没有增加新字体、渐变、彩色大卡或无意义动画；特色只花在与教师核稿直接相关的纸张工作台上。

## 3. 既有视觉 Token

优先使用语义 Tailwind 类；以下 Hex 仅作为设计/截图核对基准。

| 语义 | 亮色 | 暗色 | 用途 |
|---|---|---|---|
| 页面背景 | `#F8FAFC` | `#0F172A` | `bg-background` |
| 主文字 | `#111827` | `#E2E8F0` | `text-foreground` |
| 卡片 | `#FFFFFF` | `#1E293B` | `bg-card` |
| 次级底 | `#F1F5F9` | `#334155` | 预览工作台、只读内容 |
| 次级文字 | `#64748B` | `#94A3B8` | 文件名、状态说明 |
| 边框 | `#D9E0E9` | `#475569` | `border` |
| 主色 | `#2563EB` | `#818CF8` | 打开态、焦点、对照脊线 |
| 成功 | `#0F766E` | `#2DD4BF` | 文件可用 |
| 警告 | `#B45309` | `#F59E0B` | 处理中、任务结束清理 |
| 错误 | `#DC2626` | `#F87171` | 读取失败 |

- 字体：`Inter, PingFang SC, Microsoft YaHei, Noto Sans SC, system-ui`，不新增字体。
- 页面标题：`28px → 30px / 36px / 700`；卡片标题 `20px / 700`；区块标题 `14px / 600–700`；元数据 `11–12px`。
- 页面宽度：`max-w-[1300px]`；主要间距 `16px`，区块间距 `20px`。
- 圆角：外层 `10px`，内层 `8–9px`，按钮 `7–8px`，状态标签胶囊形。
- 阴影：仅文件“纸张”使用 `0 8px 28px rgb(15 23 42 / 0.12)`；其他区域继续只用边框。

## 4. 桌面布局

关闭预览时页面完全不变。

```text
标题 / 返回                         Stepper
四项指标
学生切换 ─ 当前学生 ─ 修改身份 ─ [查看原文件]
搜索与键盘提示
题目导航 | 当前全部作答卡片
```

打开后，在既有指标和学生导航下面进入对照工作区：

```text
┌─ 原文件 50% ───────────────┬═ 可拖动对照脊线 ═┬─ 识别内容 50% ─────────────┐
│ 文件名 / PDF或图片 / 关闭  │                   │ 搜索 / 当前学生 / 作答卡片  │
│                            │                   │ 识别文本仍可编辑、保存        │
│ 浅灰工作台 + 白色纸张       │                   │ 原页面题目侧栏改为紧凑导航     │
└────────────────────────────┴───────────────────┴───────────────────────────┘
```

- `lg/xl`：默认 50% / 50%，中间预留 `12px` 可命中区域，中心线保持 `2px` 主色。
- 鼠标或触控笔拖动分隔条即可实时调整；比例优先限制在 35%～65%，同时保证原稿至少 `360px`、识别区至少 `480px`，空间不足时以最小宽度为准。
- 使用原生 Pointer Events 和 `setPointerCapture`，不引入新的 split-pane 依赖；拖动时只更新当前页面 React state。
- 当前页面内关闭再打开保留上次比例；刷新或重新进入页面恢复 50% / 50%，不写 `localStorage` 或后端。
- 左侧 `sticky top-[86px]`，最大高度 `calc(100vh - 102px)`。
- 预览打开时隐藏原有左侧题目导航，避免形成三栏；保留搜索、上一/下一题按钮和卡片内导航。
- 关闭时恢复原有 `180–240px + content` 两栏，不改变滚动位置、当前题目或草稿。

题目资料审核页复用同一布局；右侧仍是当前 `QuestionPackageCard` 列表。

## 5. 平板与手机

- `< lg`：同页纵向堆叠，预览在识别内容上方；隐藏分隔条，不弹 modal，不跳新页。
- 平板预览高度 `52vh`；手机 `42vh`、最小 `280px`。
- 预览标题栏在预览容器内 sticky；关闭按钮始终可见。
- 图片使用 `object-contain`；PDF 默认用 `pdfjs-dist` 渲染为同页 Canvas，渲染失败时才降级到浏览器 PDF 查看器。
- 手机不承诺同时看到完整两侧，但切换/滚动不丢编辑草稿。

## 6. 入口位置

### 学生作答页

- 在 `StudentNavigation` 最右操作区加入短按钮：`Eye + 查看原文件`。
- 文件名旁保留 `FileText`；点击文件名或按钮均打开同一面板。
- 可用时使用 `Button variant="secondary"`；打开时增加 `border-primary/30 bg-primary/5 text-primary`。

### 题目资料页

- 页面标题右侧增加 `查看题目原文件`；每张题卡不重复放相同按钮。
- 这样不会让十几道题重复出现同一操作，也符合“整份题目文件”的产品范围。

## 7. 状态合同

Week 1 放在独立 `sourcePreview.ts`，不扩大共享 `Task` DTO；本任务只冻结可供 lyj 确认的 mock 字段。

```ts
type SourceFileStatus = "available" | "processing" | "unavailable";
type SourcePreviewKind = "pdf" | "image" | "unsupported";
type SourceUnavailableReason =
  | "task_finalized"
  | "unsupported_type"
  | "not_persisted"
  | "missing";

interface SourceFileDescriptor {
  file_id: string;
  display_name: string;
  mime_type: string;
  status: SourceFileStatus;
  preview_kind: SourcePreviewKind;
  unavailable_reason?: SourceUnavailableReason | null;
}
```

约束：DTO 只有 `file_id`，绝不包含服务器路径、storage key 或永久公开 URL。真实鉴权 blob 读取由后续 Week 2 任务实现。

## 8. 七类可见状态

| 状态 | 入口 | 面板 | 主要文案/操作 |
|---|---|---|---|
| PDF 可用 | 可点 | pdf.js 多页 Canvas（失败时浏览器查看器兜底） | `查看原文件`、`关闭对照` |
| 图片可用 | 可点 | `img object-contain` | 显示完整图片，不裁切 |
| 首次加载 | 按钮 loading | Spinner/Skeleton | `正在读取原文件…` |
| 处理中 | 可点并带 Loader | 中性等待态 | `文件仍在处理中，稍后重试` |
| 读取失败 | 可点 | `InlineNotice danger` | `重新读取`、`关闭` |
| 不支持/缺失 | 禁用 + `HelpTooltip` | 不打开 | 说明支持 PDF/常见图片 |
| 任务结束已清理 | 禁用 + `HelpTooltip` | 不打开 | `线上临时副本已不再保存；本地文件未受影响` |

读取失败与 `unavailable` 必须区分：前者可重试，后者是后端权威生命周期状态。

## 9. 组件复用与新增

### 直接复用

- `Button`：打开、关闭、重试。
- `InlineNotice`：processing/error/unavailable 解释。
- `HelpTooltip`：禁用原因。
- `MarkdownMath`：右侧识别内容保持原实现。
- `NewTaskStepper`、现有 `StudentNavigation`、`QuestionNavigator`。
- `lucide-react`：`Eye`、`X`、`FileText`、`Image`、`LoaderCircle`、`RefreshCw`、`TriangleAlert`。

### 新增

- `components/tasks/OriginalFilePreviewPanel.tsx`：面板、文件画布、全部状态。
- `components/tasks/PdfDocumentPreview.tsx`：仅在打开 PDF 时懒加载，多页 Canvas、读取/渲染失败重试与浏览器查看器兜底；固定 `pdfjs-dist@6.2.108`。
- `components/tasks/OriginalFilePreviewTrigger.tsx`：短按钮和禁用 tooltip。
- `components/tasks/SourceComparisonWorkspace.tsx`：50/50 布局、比例状态、分隔条和响应式降级。
- `types/sourcePreview.ts`：独立合同。
- `lib/sourcePreview.ts`：MIME→kind、reason→文案映射、object URL 清理 helper。
- `mocks/sourcePreview.ts`：仅开发/测试环境的 PDF/图片/状态 fixture。

## 10. Mock 策略

- 只在 `import.meta.env.DEV` 或测试模式启用；生产构建没有打开 mock 的环境变量逃生口。
- 可用查询参数切状态：`sourcePreview=pdf|image|processing|unavailable|error`，生产构建忽略。
- 不使用“接口 404 自动伪装成功”；真实环境缺接口时必须显示不可用。
- Mock 不设置 auth、不写 Task 数据、不模拟 owner 授权。

## 11. 交互与可访问性

- 打开后将焦点移到面板关闭按钮；关闭后返回原触发按钮。
- 面板使用 `section aria-labelledby`，加载状态 `role="status"`，错误 `role="alert"`。
- 分隔条使用 `role="separator"`、`aria-orientation="vertical"`、`aria-valuemin/max/now` 和可见焦点环。
- 拖动、`←/→` 每次 2%、`Shift + ←/→` 每次 10% 均可调节；`Home/End` 到边界，双击恢复 50%。
- 拖动期间使用 `cursor-col-resize` 并阻止误选文字；结束拖动立即恢复文本选择。
- 禁用按钮旁提供可聚焦 `HelpTooltip`；不能只靠 hover。
- `Escape` 仅在焦点位于预览区时关闭，不抢文本编辑快捷键。
- 切学生时若预览已打开，保持打开并加载新学生文件；先撤销旧 object URL。
- 切题时题目原文件不变；不重复请求。
- 遵守 `prefers-reduced-motion`；只用现有短 transition，不做滑入动画。
- PDF Canvas 使用 `role=document` 与逐页可读标签；浏览器 object 兜底必须有明确 `title`；图片 `alt` 使用安全显示名。
- `OriginalFilePreviewPanel` 可接收可选 provenance note，但正式 Week 1 mock 不伪造来源证明；后续只有真实合同提供证据时才展示。

## 12. 文件改动边界

### GSY-W1-PREVIEW-UI（本任务）

- 改 `routes/tasks/StudentAnswerReviewPage.tsx`：入口、对照布局、保持编辑状态。
- 改 `routes/tasks/QuestionPreparationDetailPage.tsx`：题目入口和复用布局。
- 改 `i18n/messages.ts`：中英双语状态与按钮文案。
- 新增第 9 节组件/types/lib/mock/tests。
- 不改 `api/tasks.ts`、`api/hooks/tasks.ts`、`types/task.ts`，减少与并行 PR 的共享文件冲突。

### 后续 GSY-W2-PREVIEW-AND-REGISTRATION（不属于本任务）

- 新增 `api/sourceFiles.ts` 与 `api/hooks/sourceFiles.ts`。
- 将 mock descriptor 映射替换为 lyj 的真实 status + 鉴权 blob。
- 保留组件 public props，不改视觉层。

## 13. 实施顺序

1. 冻结第 7 节合同及中英文文案，lyj 只确认字段含义。
2. 实现 `SourceComparisonWorkspace`：默认 50/50、拖动/键盘调节、边界和移动端降级。
3. 实现纯展示 `OriginalFilePreviewPanel` 和七类组件测试。
4. 接学生作答页，验证草稿、切学生、关闭后滚动位置及比例保留。
5. 接题目资料页，共用组件，不复制状态逻辑。
6. 做 `1440×900`、`1280×720`、`768×1024`、`390×844` 亮/暗色检查。
7. 运行定向测试、全量前端测试、scope audit、typecheck、production build。

## 14. 测试与验收

- PDF、图片、processing、unavailable、finalized、error/retry 七种状态逐项测试。
- 打开/关闭不清除作答草稿；未保存离开拦截仍生效。
- 桌面首次打开为 50/50；鼠标拖动、键盘调节、35%～65%/最小宽度约束和双击复位均有测试。
- `< lg` 不出现分隔条或横向拖动手势，页面自然上下堆叠。
- 切学生时 source descriptor 更新且旧 object URL 被 revoke。
- 不支持文件无法加载 active content，禁用原因键盘可读。
- 手机无横向页面溢出；PDF/图片不越出面板。
- 深色模式、中文、英文、200% zoom、键盘 focus 均可用。
- `npm test -- OriginalFilePreviewPanel StudentAnswerReviewPage QuestionPreparationDetailPage`
- `npm run lint && npm run build`

## 15. 完成定义

- 教师在正式复核页内打开/关闭整份 PDF 或图片，并继续编辑右侧文字。
- 所有不可用状态都有明确原因和下一步，不出现空白/死按钮。
- 无服务器路径、永久 URL、主动内容渲染或前端 owner 判断。
- 默认关闭时现有页面像素与交互不变；打开时才进入对照布局。
- Week 1 有 50/50 可拖动 mock 的截图/录屏、测试结果和 gsy 自测记录；lyj 只需给状态合同确认。
- 不以 lyj 的真实文件接口或 gxr 的真实接口验收作为 `GSY-W1-PREVIEW-UI` 完成前置；两者属于后续 Week 2 任务。
