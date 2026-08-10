import {
  ArrowRight,
  BarChart3,
  Bot,
  Braces,
  Check,
  ChevronRight,
  Code2,
  Eye,
  FileCheck2,
  FileImage,
  FileText,
  MessageCircle,
  ScanText,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import "./frontier.css";

function walkthroughStages(locale: Locale) {
  return [
  {
    id: "source",
    label: tx(locale, "原文件", "Source"),
    title: tx(locale, "让原文件始终触手可及。", "Keep the original within reach."),
    description: tx(locale, "在每份识别结果旁打开完整作答，而不是盲信脱离原文的文本。", "Open the complete submission beside every recognized answer instead of trusting detached text."),
    metric: tx(locale, "1 份原件 · 4 道作答", "1 source · 4 answers"),
  },
  {
    id: "recognize",
    label: tx(locale, "识别", "Recognize"),
    title: tx(locale, "呈现不确定性，而不是隐藏它。", "Surface uncertainty, do not hide it."),
    description: tx(locale, "手写 μ 作为证据保留，并明确标记给教师快速核对。", "The handwritten μ is preserved as evidence and marked for a quick teacher check."),
    metric: tx(locale, "原文置信度 82%", "82% source confidence"),
  },
  {
    id: "grade",
    label: tx(locale, "批改", "Grade"),
    title: tx(locale, "让每一分都有评分依据。", "Tie every point to a rubric signal."),
    description: tx(locale, "数学推理、物理单位和代码测试在同一个可复核空间中完成。", "Math reasoning, physics units, and code tests share one reviewable scoring workspace."),
    metric: tx(locale, "命中 3 项评分依据", "3 rubric signals found"),
  },
  {
    id: "analyze",
    label: tx(locale, "分析", "Analyze"),
    title: tx(locale, "从一次批改，追问到班级洞察。", "Turn one grading run into class insight."),
    description: tx(locale, "用自然语言继续提问，并生成始终关联到题目与评分依据的新图表。", "Ask follow-up questions in natural language and generate new charts that stay linked to questions and rubric evidence."),
    metric: tx(locale, "已生成 1 个新图表", "1 new chart generated"),
  },
  ] as const;
}

function insightStories(locale: Locale) {
  return [
    {
      id: "reteach",
      query: tx(locale, "哪道题最需要重新讲解？", "Which question needs reteaching?"),
      answer: tx(locale, "Q3 的得分率最低，主要缺口是反向包含关系中的范数恒等式。", "Q3 has the lowest attainment. The main gap is the norm identity in the reverse inclusion."),
      title: tx(locale, "按题得分率", "Attainment by question"),
      bars: [
        { label: "Q1 · Calculus", value: 87, highlight: false },
        { label: "Q2 · Mechanics", value: 71, highlight: false },
        { label: "Q3 · Proof", value: 50, highlight: true },
        { label: "Q4 · Python", value: 78, highlight: false },
      ],
    },
    {
      id: "format",
      query: tx(locale, "手写与排版作答的表现有何不同？", "How do handwritten and typeset submissions differ?"),
      answer: tx(locale, "手写作答的推理得分接近，但需要教师复核的识别信号约为排版作答的 2.4 倍。", "Reasoning scores are close, but handwritten work produces about 2.4× more recognition signals for teacher review."),
      title: tx(locale, "平均得分与复核信号", "Average score and review signals"),
      bars: [
        { label: tx(locale, "手写 · 得分", "Handwritten · score"), value: 76, highlight: false },
        { label: tx(locale, "排版 · 得分", "Typeset · score"), value: 82, highlight: false },
        { label: tx(locale, "手写 · 复核", "Handwritten · review"), value: 62, highlight: true },
        { label: tx(locale, "排版 · 复核", "Typeset · review"), value: 26, highlight: false },
      ],
    },
    {
      id: "code",
      query: tx(locale, "编程题最常失败在哪个隐藏测试？", "Which hidden code test fails most often?"),
      answer: tx(locale, "空输入处理是最常见缺口；其次是对接近 1000 的输入未先减去最大值。", "Empty-input handling is the most common gap, followed by failing to subtract the maximum for values near 1000."),
      title: tx(locale, "隐藏测试通过率", "Hidden-test pass rate"),
      bars: [
        { label: tx(locale, "空输入", "Empty input"), value: 43, highlight: true },
        { label: tx(locale, "极端数值", "Extreme values"), value: 61, highlight: false },
        { label: tx(locale, "均衡输入", "Balanced input"), value: 91, highlight: false },
      ],
    },
  ] as const;
}

type WalkthroughStageId = ReturnType<typeof walkthroughStages>[number]["id"];

const liveDemoEntryHref = "/frontier/enter";

export function FrontierLandingPage() {
  const { locale } = useI18n();
  const stages = walkthroughStages(locale);
  const insights = insightStories(locale);
  const [activeStage, setActiveStage] = useState(0);
  const [activeInsight, setActiveInsight] = useState(0);
  const stage = stages[activeStage];
  const insight = insights[activeInsight];

  useEffect(() => {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) return;
    const timer = window.setInterval(() => {
      setActiveStage((current) => (current + 1) % stages.length);
    }, 4200);
    return () => window.clearInterval(timer);
  }, []);

  function chooseStage(index: number) {
    setActiveStage(index);
  }

  return (
    <div className="frontier-site">
      <a className="frontier-skip" href="#frontier-main">{tx(locale, "跳到主要内容", "Skip to content")}</a>
      <header className="frontier-nav">
        <Link className="frontier-wordmark" to="/frontier" aria-label="SmarTAI Frontier home">
          <span className="frontier-wordmark-mark" aria-hidden="true">S</span>
          <span>SmarTAI</span>
        </Link>
        <nav aria-label={tx(locale, "展示页导航", "Showcase navigation")}>
          <a href="#how-it-works">{tx(locale, "工作方式", "How it works")}</a>
          <a href="#ask-smartai">Ask SmarTAI</a>
          <a href="#trust">{tx(locale, "可信设计", "Trust by design")}</a>
        </nav>
        <div className="frontier-nav-actions">
          <LanguageToggle className="frontier-language-toggle border-slate-300 bg-white/75" />
          <Link className="frontier-nav-cta" to={liveDemoEntryHref}>
            {tx(locale, "进入真实 Demo", "Enter live demo")}
            <ArrowRight aria-hidden="true" />
          </Link>
        </div>
      </header>

      <main id="frontier-main">
        <section className="frontier-hero">
          <div className="frontier-hero-copy">
            <div className="frontier-kicker">
              <span className="frontier-kicker-dot" aria-hidden="true" />
              AWS From Idea to Frontier · {tx(locale, "报名展示", "Application demo")}
            </div>
            <h1>{tx(locale, "从成堆作业，到可追溯的智能初评。", "From a stack of coursework to an auditable first pass.")}</h1>
            <p className="frontier-hero-lede">
              {tx(locale, "SmarTAI 帮助理工科教师复核数学、物理与编程混合作业，同时始终保留原始作答证据与最终评分权。", "SmarTAI helps STEM educators review mixed mathematics, physics, and programming work without losing sight of the original submission—or control of the final grade.")}
            </p>
            <div className="frontier-hero-actions">
              <Link className="frontier-primary-cta" to={liveDemoEntryHref}>
                {tx(locale, "进入真实 Demo", "Enter the live demo")}
                <ArrowRight aria-hidden="true" />
              </Link>
              <a className="frontier-secondary-cta" href="#walkthrough" onClick={() => chooseStage(1)}>
                {tx(locale, "查看手写作答", "Review a handwritten answer")}
                <Eye aria-hidden="true" />
              </a>
            </div>
            <div className="frontier-proof-strip" aria-label={tx(locale, "Demo 边界", "Demo boundaries")}>
              <span><Check aria-hidden="true" /> {tx(locale, "合成学生数据", "Synthetic student data")}</span>
              <span><Check aria-hidden="true" /> {tx(locale, "真实产品流程", "Real product workflow")}</span>
              <span><Check aria-hidden="true" /> {tx(locale, "教师控制发布", "Teacher-controlled release")}</span>
            </div>
          </div>

          <div className="frontier-hero-visual" data-stage={stage.id} aria-label={tx(locale, "SmarTAI 互动产品导览", "Interactive SmarTAI product walkthrough")}>
            <div className="frontier-visual-caption">
              <span>{tx(locale, "产品导览", "Product walkthrough")}</span>
              <span>{tx(locale, "合成内容", "Synthetic content")}</span>
            </div>
            <div className="frontier-document-stack" aria-hidden="true">
              <div className="frontier-page frontier-page-back" />
              <div className="frontier-page frontier-page-middle" />
              <div className="frontier-source-page">
                <img src="/frontier-demo/DEMO-002_handwritten.png" alt="" />
                <span className="frontier-source-focus" />
              </div>
              <div className="frontier-source-types">
                <span><FileText /> PDF + LaTeX</span>
                <span><FileImage /> {tx(locale, "手写扫描", "Handwritten scan")}</span>
                <span><Braces /> Python</span>
              </div>
              <div className="frontier-grade-stamp">
                <strong>6 / 8</strong>
                <span>{tx(locale, "等待教师复核", "Teacher review")}</span>
              </div>
            </div>
            <div className="frontier-analysis-board" aria-hidden="true">
              <div className="frontier-analysis-board-head">
                <span>{tx(locale, "班级分析", "Class analysis")}</span>
                <strong>{tx(locale, "按题关联", "Question-linked")}</strong>
              </div>
              <p>{tx(locale, "哪道题最需要重新讲解？", "Which question needs reteaching?")}</p>
              <div className="frontier-analysis-bars">
                {[87, 71, 50, 78].map((value, index) => (
                  <span key={value} style={{ "--bar-value": `${value}%`, "--bar-delay": `${index * 90}ms` } as CSSProperties}>
                    <i />
                    <small>Q{index + 1}</small>
                  </span>
                ))}
              </div>
              <div className="frontier-analysis-callout">
                <Sparkles />
                <span>{tx(locale, "Q3 · 范数恒等式是主要缺口", "Q3 · norm identity is the main gap")}</span>
              </div>
            </div>
            <div className="frontier-reasoning-rail" aria-hidden="true">
              <span className="frontier-rail-line" />
              <span className="frontier-rail-pulse" />
            </div>
            <div className="frontier-review-card" aria-live="polite">
              <div className="frontier-review-card-topline">
                <span>{stage.label}</span>
                <strong>{String(activeStage + 1).padStart(2, "0")} / 04</strong>
              </div>
              <h2>{stage.title}</h2>
              <p>{stage.description}</p>
              <WalkthroughStageDetail key={stage.id} id={stage.id} locale={locale} metric={stage.metric} />
            </div>
            <div className="frontier-stage-tabs" role="tablist" aria-label={tx(locale, "导览阶段", "Walkthrough stages")}>
              {stages.map((item, index) => (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  aria-selected={activeStage === index}
                  onClick={() => chooseStage(index)}
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  {item.label}
                </button>
              ))}
            </div>
          </div>
        </section>

        <section id="how-it-works" className="frontier-process-section">
          <div className="frontier-section-intro">
            <p className="frontier-eyebrow">{tx(locale, "一条可复核的工作流", "One reviewable workflow")}</p>
            <h2>{tx(locale, "不同类型的推理，在同一个地方核验。", "Different kinds of reasoning. One place to verify them.")}</h2>
            <p>{tx(locale, "每一步都与原文、评分标准和教师的后续决定保持连接。", "Every step stays connected to the source, the rubric, and the teacher decision that follows.")}</p>
          </div>
          <div className="frontier-process-grid">
            <ProcessCard icon={<FileText />} title={tx(locale, "导入混合作业", "Import mixed work")} copy={tx(locale, "在同一任务中处理排版 PDF、合成扫描件、公式、证明和代码。", "Start with typeset PDFs, synthetic scans, formulas, proofs, and code in the same task.")} label={tx(locale, "原文导入", "Source intake")} />
            <ProcessCard icon={<ScanText />} title={tx(locale, "复核识别结果", "Review recognition")} copy={tx(locale, "将完整原件与结构化题目和作答并排对照，包括低置信区域。", "Compare the complete original with structured questions and answers, including low-confidence regions.")} label={tx(locale, "原文证据", "Source evidence")} />
            <ProcessCard icon={<Code2 />} title={tx(locale, "评估推理过程", "Evaluate reasoning")} copy={tx(locale, "按题应用评分标准、数值校验和冻结的代码测试结果。", "Apply question-specific rubrics, numerical checks, and frozen code-test outcomes where appropriate.")} label={tx(locale, "理工科复核", "STEM-aware review")} />
            <ProcessCard icon={<MessageCircle />} title={tx(locale, "追问班级数据", "Ask your class data")} copy={tx(locale, "用自然语言继续分析，并生成与具体题目和评分依据相连的新图表。", "Continue in natural language and generate new charts linked to specific questions and rubric evidence.")} label={tx(locale, "自然语言分析", "Natural-language analysis")} />
          </div>
        </section>

        <section id="walkthrough" className="frontier-workspace-section">
          <div className="frontier-workspace-copy">
            <p className="frontier-eyebrow">{tx(locale, "源自真实产品界面", "Built from the product UI")}</p>
            <h2>{tx(locale, "识别完成后，原文件不会消失。", "The original file does not disappear after recognition.")}</h2>
            <p>
              {tx(locale, "真实 Demo 使用这里展示的同一套任务工作台、原文对照、复核语言和评分控件。动画面板是前端导览；按钮将进入真实 API 驱动的产品。", "The live demo uses the same task workspace, source comparison, review language, and score controls shown here. The animated panel is a walkthrough; the button opens the real API-backed product.")}
            </p>
            <ul>
              <li><ShieldCheck aria-hidden="true" /> {tx(locale, "原文优先的可追溯性", "Source-first traceability")}</li>
              <li><Sparkles aria-hidden="true" /> {tx(locale, "将不确定性转化为复核队列", "Uncertainty becomes a review queue")}</li>
              <li><BarChart3 aria-hidden="true" /> {tx(locale, "班级洞察始终关联具体题目", "Class insight remains linked to questions")}</li>
            </ul>
            <Link className="frontier-inline-link" to={liveDemoEntryHref}>
              {tx(locale, "进入真实工作台", "Continue in the live workspace")}
              <ChevronRight aria-hidden="true" />
            </Link>
          </div>
          <div className="frontier-workspace-window" aria-label={tx(locale, "SmarTAI 原文对照预览", "SmarTAI source comparison preview")}>
            <div className="frontier-window-bar">
              <span className="frontier-window-brand">SmarTAI</span>
              <span>{tx(locale, "校对学生作答", "Review Student Answers")}</span>
              <span className="frontier-demo-badge">{tx(locale, "产品导览", "Product walkthrough")}</span>
            </div>
            <div className="frontier-window-body">
              <div className="frontier-window-source">
                <div className="frontier-window-label"><FileText aria-hidden="true" /> {tx(locale, "原文件", "Original file")}</div>
                <img src="/frontier-demo/DEMO-002_handwritten.png" alt={tx(locale, "带教师批注的合成手写力学作答", "Synthetic handwritten mechanics answer with teacher annotations")} />
              </div>
              <div className="frontier-window-divider" aria-hidden="true" />
              <div className="frontier-window-review">
                <div className="frontier-window-label"><ScanText aria-hidden="true" /> {tx(locale, "识别作答", "Recognized answer")}</div>
                <span className="frontier-attention-pill">{tx(locale, "需要核对 1 项", "Needs one check")}</span>
                <h3>{tx(locale, "Q2 · 粗糙斜面运动", "Q2 · Motion down a rough incline")}</h3>
                <p className="frontier-recognized-answer">μ = 0.20; a = 3.20 m/s²; v = 4.38 m/s</p>
                <div className="frontier-evidence-note">
                  <strong>{tx(locale, "为何被标记", "Why this is flagged")}</strong>
                  <span>{tx(locale, "手写识别曾将 μ 读作“u”。请在确认前打开原文件。", "The handwriting model briefly read μ as “u”. Open the source before confirming.")}</span>
                </div>
                <div className="frontier-score-row">
                  <span>{tx(locale, "建议得分", "Suggested score")}</span>
                  <strong>6 / 8</strong>
                </div>
                <button type="button" onClick={() => chooseStage(3)}>{tx(locale, "确认教师决定", "Confirm teacher decision")}</button>
              </div>
            </div>
          </div>
        </section>

        <section id="ask-smartai" className="frontier-ask-section">
          <div className="frontier-ask-heading">
            <div>
              <p className="frontier-eyebrow">Ask SmarTAI</p>
              <h2>{tx(locale, "批改结束，不代表分析结束。", "Grading ends. Inquiry does not.")}</h2>
            </div>
            <p>{tx(locale, "教师可以随时用自然语言追问当前班级数据。SmarTAI 将回答转化为可核对的解释和新图表，并保留题目层面的来源。", "Teachers can question the current class data in natural language at any time. SmarTAI turns the answer into a reviewable explanation and a new chart while preserving question-level provenance.")}</p>
          </div>

          <div className="frontier-ask-workbench">
            <div className="frontier-ask-console">
              <div className="frontier-ask-console-title">
                <span><Bot aria-hidden="true" /> Ask SmarTAI</span>
                <small>{tx(locale, "产品导览 · 合成数据", "Product walkthrough · synthetic data")}</small>
              </div>
              <div className="frontier-ask-prompts" aria-label={tx(locale, "分析问题示例", "Example analysis questions")}>
                {insights.map((item, index) => (
                  <button
                    key={item.id}
                    type="button"
                    aria-pressed={activeInsight === index}
                    onClick={() => setActiveInsight(index)}
                  >
                    {item.query}
                  </button>
                ))}
              </div>
              <div className="frontier-conversation">
                <div className="frontier-teacher-query">
                  <span>{tx(locale, "教师", "Teacher")}</span>
                  <p>{insight.query}</p>
                </div>
                <div className="frontier-smartai-answer" key={insight.id}>
                  <span><Sparkles aria-hidden="true" /> SmarTAI</span>
                  <p>{insight.answer}</p>
                  <small>{tx(locale, "依据：4 道题 · 4 份合成作答 · 当前评分版本", "Based on 4 questions · 4 synthetic submissions · current grading version")}</small>
                </div>
              </div>
            </div>

            <div className="frontier-generated-chart" key={`${insight.id}-chart`}>
              <div className="frontier-chart-head">
                <div>
                  <span>{tx(locale, "即时生成的图表", "Chart generated on demand")}</span>
                  <h3>{insight.title}</h3>
                </div>
                <span className="frontier-chart-badge"><BarChart3 aria-hidden="true" /> {tx(locale, "关联题目", "Question-linked")}</span>
              </div>
              <div className="frontier-chart-plot">
                {insight.bars.map((bar, index) => (
                  <div className={`frontier-chart-row${bar.highlight ? " is-highlight" : ""}`} key={bar.label}>
                    <span>{bar.label}</span>
                    <div><i style={{ "--bar-value": `${bar.value}%`, "--bar-delay": `${index * 100}ms` } as CSSProperties} /></div>
                    <strong>{bar.value}%</strong>
                  </div>
                ))}
              </div>
              <div className="frontier-chart-foot">
                <ShieldCheck aria-hidden="true" />
                <span>{tx(locale, "点击图表可回到对应题目、评分依据与教师确认版本。", "Each mark can trace back to the question, rubric evidence, and teacher-confirmed version.")}</span>
              </div>
            </div>
          </div>
        </section>

        <section id="trust" className="frontier-trust-section">
          <div>
            <p className="frontier-eyebrow">{tx(locale, "可信设计", "Trust by design")}</p>
            <h2>{tx(locale, "AI 提议，证据解释，教师决定。", "AI proposes. Evidence explains. Teachers decide.")}</h2>
          </div>
          <div className="frontier-trust-list">
            <article><span>01</span><h3>{tx(locale, "追溯原文", "Trace the source")}</h3><p>{tx(locale, "原文件与识别内容在整个复核过程中保持连接。", "Original files and recognized content stay connected through review.")}</p></article>
            <article><span>02</span><h3>{tx(locale, "呈现不确定性", "Expose uncertainty")}</h3><p>{tx(locale, "低置信度和模型分歧会转化为明确的教师操作。", "Low confidence and model disagreement become explicit teacher actions.")}</p></article>
            <article><span>03</span><h3>{tx(locale, "控制发布", "Control the release")}</h3><p>{tx(locale, "教师确认最终结果前，任何分数都不会发布给学生。", "No score reaches a student until the educator confirms the final result.")}</p></article>
          </div>
        </section>

        <section className="frontier-final-cta">
          <div>
            <p className="frontier-eyebrow">{tx(locale, "准备查看真实工作流？", "Ready to inspect the real workflow?")}</p>
            <h2>{tx(locale, "打开 SmarTAI 真实 Demo。", "Open the live SmarTAI demo.")}</h2>
            <p>{tx(locale, "真实工作台使用合成文件，并运行真实 API 与批改流程。", "The live workspace uses synthetic files with the real API and grading pipeline.")}</p>
          </div>
          <Link className="frontier-primary-cta frontier-primary-cta-light" to={liveDemoEntryHref}>
            {tx(locale, "进入真实 Demo", "Enter live demo")}
            <ArrowRight aria-hidden="true" />
          </Link>
        </section>
      </main>

      <footer className="frontier-footer">
        <Link className="frontier-wordmark" to="/frontier"><span className="frontier-wordmark-mark" aria-hidden="true">S</span><span>SmarTAI</span></Link>
        <p>{tx(locale, "互动展示 · 合成学生数据 · 点击即可进入真实产品", "Interactive showcase · synthetic student data · live product available after entry")}</p>
      </footer>
    </div>
  );
}

function tx(locale: Locale, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

function ProcessCard({ icon, title, copy, label }: { icon: ReactNode; title: string; copy: string; label: string }) {
  return (
    <article className="frontier-process-card">
      <div className="frontier-process-icon" aria-hidden="true">{icon}</div>
      <p>{label}</p>
      <h3>{title}</h3>
      <span>{copy}</span>
    </article>
  );
}

function WalkthroughStageDetail({ id, locale, metric }: { id: WalkthroughStageId; locale: Locale; metric: string }) {
  if (id === "source") {
    return (
      <div className="frontier-review-sample frontier-intake-sample">
        <div className="frontier-sample-heading">
          <span>{tx(locale, "本次任务", "Current task")}</span>
          <span>{metric}</span>
        </div>
        <div className="frontier-intake-list">
          <span><FileText aria-hidden="true" /><i><strong>question_source.pdf</strong><small>{tx(locale, "排版题目 · 4 题", "Typeset source · 4 questions")}</small></i><Check aria-hidden="true" /></span>
          <span><FileImage aria-hidden="true" /><i><strong>DEMO-002.pdf</strong><small>{tx(locale, "手写作答 · 需要视觉识别", "Handwritten work · vision required")}</small></i><Check aria-hidden="true" /></span>
          <span><Braces aria-hidden="true" /><i><strong>stable_softmax.py</strong><small>{tx(locale, "代码与隐藏测试", "Code and hidden tests")}</small></i><Check aria-hidden="true" /></span>
        </div>
      </div>
    );
  }

  if (id === "recognize") {
    return (
      <div className="frontier-review-sample frontier-recognition-sample">
        <div className="frontier-sample-heading">
          <span>{tx(locale, "Q2 · 力学", "Q2 · Mechanics")}</span>
          <span className="is-warning">{metric}</span>
        </div>
        <div className="frontier-equation">a = g(sin 30° − μ cos 30°)</div>
        <div className="frontier-recognition-diff">
          <span><small>{tx(locale, "原文件字形", "Source glyph")}</small><strong>μ</strong></span>
          <span><small>{tx(locale, "候选识别", "OCR candidate")}</small><strong>u</strong></span>
          <span className="is-flagged"><small>{tx(locale, "下一步", "Next action")}</small><strong>{tx(locale, "教师核对", "Teacher check")}</strong></span>
        </div>
      </div>
    );
  }

  if (id === "grade") {
    return (
      <div className="frontier-review-sample frontier-grading-sample">
        <div className="frontier-sample-heading">
          <span>{tx(locale, "Q2 · 评分依据", "Q2 · Rubric evidence")}</span>
          <span>{metric}</span>
        </div>
        <div className="frontier-grade-summary">
          <strong>6 <small>/ 8</small></strong>
          <span><FileCheck2 aria-hidden="true" /> {tx(locale, "建议初评分", "Suggested first pass")}</span>
        </div>
        <div className="frontier-rubric-lines">
          <span className="is-complete"><Check aria-hidden="true" /> {tx(locale, "受力方向正确", "force direction correct")}</span>
          <span className="is-complete"><Check aria-hidden="true" /> {tx(locale, "加速度计算正确", "acceleration correct")}</span>
          <span><Check aria-hidden="true" /> {tx(locale, "末步单位缺失", "final units missing")}</span>
        </div>
        <div className="frontier-test-signal"><Code2 aria-hidden="true" /> {tx(locale, "代码题：2 / 3 个隐藏测试通过", "Code question: 2 / 3 hidden tests passed")}</div>
      </div>
    );
  }

  return (
    <div className="frontier-review-sample frontier-analysis-sample">
      <div className="frontier-sample-heading">
        <span>Ask SmarTAI</span>
        <span>{metric}</span>
      </div>
      <div className="frontier-analysis-query"><MessageCircle aria-hidden="true" /> {tx(locale, "哪道题最需要重新讲解？", "Which question needs reteaching?")}</div>
      <div className="frontier-analysis-answer">
        <Sparkles aria-hidden="true" />
        <span>{tx(locale, "Q3 得分率最低；主要缺口是反向包含关系中的范数恒等式。", "Q3 has the lowest attainment; the main gap is the norm identity in the reverse inclusion.")}</span>
      </div>
      <div className="frontier-mini-chart" aria-hidden="true">
        {[87, 71, 50, 78].map((value, index) => (
          <span key={value}><i style={{ "--bar-value": `${value}%`, "--bar-delay": `${index * 80}ms` } as CSSProperties} /><small>Q{index + 1}</small></span>
        ))}
      </div>
    </div>
  );
}
