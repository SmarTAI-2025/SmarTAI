import {
  ArrowRight,
  BarChart3,
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
} from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { SmarTAIAppMark, SmarTAIWordmark } from "@/components/brand/SmarTAIBrand";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import "./frontier.css";

function walkthroughStages(locale: Locale) {
  return [
  {
    id: "source",
    label: tx(locale, "原文件", "Source"),
    title: tx(locale, "原文件，始终在手边。", "Keep the source within reach."),
    description: tx(locale, "在每份识别结果旁打开完整作答，而不是盲信脱离原文的文本。", "Open the complete submission beside every recognized answer instead of trusting detached text."),
    metric: tx(locale, "1 份原件 · 4 道作答", "1 source · 4 answers"),
  },
  {
    id: "recognize",
    label: tx(locale, "识别", "Recognize"),
    title: tx(locale, "原件与识别结果，并排复核。", "Review recognition beside the source."),
    description: tx(locale, "完整原件与结构化文本并排保留，教师可在批改前确认或修正。", "The complete source stays beside structured text so the teacher can confirm or correct it before grading."),
    metric: tx(locale, "原件对照已打开", "Source comparison open"),
  },
  {
    id: "grade",
    label: tx(locale, "批改", "Grade"),
    title: tx(locale, "每一分，都有评分依据。", "Tie every point to its evidence."),
    description: tx(locale, "系统按评分标准给出计算、推导、证明与代码作答的初评依据，教师负责复核。", "SmarTAI proposes a first pass for calculations, derivations, proofs, and code against the rubric; the teacher reviews it."),
    metric: tx(locale, "命中 3 项评分依据", "3 rubric signals found"),
  },
  {
    id: "analyze",
    label: tx(locale, "分析", "Analyze"),
    title: tx(locale, "从批改结果，走向班级洞察。", "Turn grading results into class insight."),
    description: tx(locale, "用自然语言追问当前评分结果，并按需生成系统支持的分析图表。", "Ask follow-up questions about the current grading results and request a supported chart when useful."),
    metric: tx(locale, "已生成 1 个新图表", "1 new chart generated"),
  },
  ] as const;
}

type InsightStory = {
  id: string;
  query: string;
  answer: string;
  followUpQuery: string;
  followUpAnswer: string;
  chartRequest: string;
  title: string;
  chart:
    | { type: "scatter"; points: Array<{ label: string; value: number; highlight?: boolean }> }
    | { type: "box"; low: number; q1: number; median: number; q3: number; high: number }
    | { type: "pie"; value: number; primaryLabel: string; secondaryLabel: string };
};

function insightStories(locale: Locale): InsightStory[] {
  return [
    {
      id: "reteach",
      query: tx(locale, "哪道题最需要重新讲解？", "Which question needs reteaching?"),
      answer: tx(locale, "Q3 的平均得分率最低，为 50%。建议先查看该题作答，再决定是否重新讲解。", "Q3 has the lowest average attainment at 50%. Review those submissions before deciding whether to reteach it."),
      followUpQuery: tx(locale, "为什么不能直接判断 Q3 没有掌握？", "Why not conclude that Q3 was not mastered?"),
      followUpAnswer: tx(locale, "得分率只能提示需要关注的位置，不能单独解释失分原因。请结合 Q3 原作答与评分依据复核。", "Attainment identifies where to look, but does not explain why points were lost. Review the original Q3 responses and rubric evidence."),
      chartRequest: tx(locale, "请把四道题的得分率画成图。", "Plot attainment for all four questions."),
      title: tx(locale, "按题得分率", "Attainment by question"),
      chart: {
        type: "scatter",
        points: [
          { label: "Q1 · Calculus", value: 87 },
          { label: "Q2 · Mechanics", value: 71 },
          { label: "Q3 · Proof", value: 50, highlight: true },
          { label: "Q4 · Python", value: 78 },
        ],
      },
    },
    {
      id: "spread",
      query: tx(locale, "班级总评成绩分布如何？", "How spread out are overall scores?"),
      answer: tx(locale, "四份合成作答从 67% 到 87%，中位数约为 77%，成绩主要集中在 70%—82%。", "The four synthetic submissions range from 67% to 87%, with a 77% median and most scores between 70% and 82%."),
      followUpQuery: tx(locale, "这能直接说明教学效果吗？", "Does that directly measure teaching effectiveness?"),
      followUpAnswer: tx(locale, "不能。这只是四份合成作答在当前评分版本下的分布，应结合原作答、样本规模与教学情境判断。", "No. This is only the distribution of four synthetic submissions under the current grading version; inspect the source work, sample size, and teaching context."),
      chartRequest: tx(locale, "请生成总评成绩箱线图。", "Generate a box plot of overall scores."),
      title: tx(locale, "总评成绩分布", "Overall score distribution"),
      chart: { type: "box", low: 67, q1: 70, median: 77, q3: 82, high: 87 },
    },
    {
      id: "threshold",
      query: tx(locale, "低于 70% 的学生占多少？", "What share of students scored below 70%?"),
      answer: tx(locale, "四份合成作答中有 1 份低于 70%，占 25%；其余 3 份达到或超过 70%。", "One of the four synthetic submissions is below 70%: 25% of the class, while three are at or above the threshold."),
      followUpQuery: tx(locale, "低于门槛就代表没有掌握吗？", "Does falling below the threshold mean no mastery?"),
      followUpAnswer: tx(locale, "不能只凭单一门槛下结论。门槛用于筛选待复核作答，教师仍需查看各题表现与原始证据。", "A single threshold is not enough for that conclusion. It filters submissions for review; the teacher still checks question-level performance and source evidence."),
      chartRequest: tx(locale, "请生成低于 70% 的占比图。", "Chart the share below 70%."),
      title: tx(locale, "成绩门槛分布", "Score threshold share"),
      chart: {
        type: "pie",
        value: 25,
        primaryLabel: tx(locale, "低于 70%", "Below 70%"),
        secondaryLabel: tx(locale, "达到或超过 70%", "At or above 70%"),
      },
    },
  ];
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
    <div className="frontier-site" data-locale={locale}>
      <a className="frontier-skip" href="#frontier-main">{tx(locale, "跳到主要内容", "Skip to content")}</a>
      <header className="frontier-nav">
        <Link className="frontier-wordmark" to="/frontier" aria-label="SmarTAI Frontier home">
          <SmarTAIWordmark alt="" className="frontier-wordmark-image" />
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
          <div className="frontier-hero-brand" aria-label="SmarTAI — Smart AI Teaching Assistant" role="img">
            <span className="frontier-hero-app-mark" aria-hidden="true">
              <SmarTAIAppMark finish="crystal-blue" />
            </span>
            <span className="frontier-hero-brand-copy">
              <SmarTAIWordmark alt="" className="frontier-hero-wordmark" />
              <span className="frontier-brand-expansion">Smart AI Teaching Assistant</span>
              <span className="frontier-hero-descriptor">{tx(locale, "面向高校理工科的 AI 智能批改与教学分析平台", "Trustworthy AI-assisted grading and learning analytics for STEM education")}</span>
            </span>
          </div>
          <div className="frontier-hero-copy">
            <div className="frontier-kicker">
              <span className="frontier-kicker-dot" aria-hidden="true" />
              {tx(locale, "SmarTAI · 互动产品展示", "SmarTAI · Interactive product showcase")}
            </div>
            <h1 className="frontier-title-fragments">
              {titleFragments(locale, ["从成堆作业，", "到可追溯初评。"], ["Faster review.", "Evidence intact."])}
            </h1>
            <p className="frontier-hero-lede">
              {tx(locale, "SmarTAI 帮助理工科教师高效复核复杂计算、公式推导、证明与编程作答，同时保留原始作答证据；最终评分与发布仍由教师决定。", "SmarTAI helps STEM educators review complex calculations, derivations, proofs, and code more efficiently while keeping original submissions in view. Teachers retain the final say on scores and release.")}
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
                <img src="/frontier-demo/live/DEMO-002_handwritten_raw.png" alt="" />
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
                <strong>{tx(locale, "当前评分结果", "Current grading results")}</strong>
              </div>
              <p>{tx(locale, "哪道题最需要重新讲解？", "Which question needs reteaching?")}</p>
              <div className="frontier-analysis-collage">
                <div className="frontier-mini-chart frontier-mini-donut-card">
                  <span className="frontier-mini-donut" aria-hidden="true"><b>25%</b></span>
                  <small>{tx(locale, "低于 70%", "Below 70%")}</small>
                </div>
                <div className="frontier-mini-chart frontier-mini-histogram-card">
                  <div className="frontier-mini-histogram" aria-hidden="true">
                    {[42, 68, 88, 62, 35].map((height, index) => <i key={height} style={{ "--bar-height": `${height}%`, "--bar-delay": `${index * 70}ms` } as CSSProperties} />)}
                  </div>
                  <small>{tx(locale, "总评分布", "Score distribution")}</small>
                </div>
                <div className="frontier-mini-chart frontier-mini-box-card">
                  <div className="frontier-mini-box" aria-hidden="true">
                    <i className="frontier-mini-box-whisker" />
                    <i className="frontier-mini-box-range" />
                    <b />
                  </div>
                  <span><small>{tx(locale, "中位数", "Median")}</small><strong>77%</strong></span>
                  <span><small>{tx(locale, "主要区间", "Middle range")}</small><strong>70–82%</strong></span>
                </div>
              </div>
              <div className="frontier-analysis-callout">
                <Eye aria-hidden="true" />
                <span>{tx(locale, "Q3 · 平均得分率最低，建议教师查看作答", "Q3 · lowest average attainment; review submissions")}</span>
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
              <h2 className="frontier-title-fragments">{stageHeading(stage.id, locale, stage.title)}</h2>
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
            <h2 className="frontier-title-fragments">
              {titleFragments(locale, ["理工科推理，", "一套可复核流程。"], ["One reviewable workflow", "for STEM reasoning."])}
            </h2>
            <p>{tx(locale, "系统提供识别与初评依据；教师随时查看原文、修改结果并决定是否发布。", "SmarTAI provides recognition and first-pass evidence; teachers inspect the source, revise results, and decide whether to release them.")}</p>
          </div>
          <div className="frontier-process-grid">
            <ProcessCard icon={<FileText />} title={tx(locale, "导入理工作答", "Import STEM work")} copy={tx(locale, "接收包含复杂计算、公式推导、证明或代码的排版 PDF 与扫描件。", "Accept typeset PDFs and scans containing complex calculations, derivations, proofs, or code.")} label={tx(locale, "原文导入", "Source intake")} />
            <ProcessCard icon={<ScanText />} title={tx(locale, "复核识别结果", "Review recognition")} copy={tx(locale, "将完整原件与结构化题目和作答并排对照；教师可在批改前确认或修改。", "Compare the complete original with structured questions and answers, then confirm or edit before grading.")} label={tx(locale, "原文证据", "Source evidence")} />
            <ProcessCard icon={<Code2 />} title={tx(locale, "复核推理过程", "Review reasoning")} copy={tx(locale, "系统按题给出评分标准匹配、数值校验与代码测试结果，教师复核后确认。", "SmarTAI proposes rubric matches, numerical checks, and code-test outcomes for teacher review.")} label={tx(locale, "理工科复核", "STEM-aware review")} />
            <ProcessCard icon={<MessageCircle />} title={tx(locale, "追问班级数据", "Ask your class data")} copy={tx(locale, "基于当前评分结果继续提问，并按需生成散点图、箱线图或饼图。", "Ask about the current grading results and request a scatter, box, or pie chart when useful.")} label={tx(locale, "自然语言分析", "Natural-language analysis")} />
          </div>
        </section>

        <section id="walkthrough" className="frontier-workspace-section">
          <div className="frontier-workspace-copy">
            <p className="frontier-eyebrow">{tx(locale, "源自真实产品界面", "Built from the product UI")}</p>
            <h2 className="frontier-title-fragments">
              {titleFragments(locale, ["识别完成，", "原文件仍在手边。"], ["Recognition done.", "Source in view."])}
            </h2>
            <p>
              {tx(locale, "真实 Demo 使用这里展示的同一套任务工作台、原文对照、复核语言和评分控件。动画面板是前端导览；按钮将进入真实 API 驱动的产品。", "The live demo uses the same task workspace, source comparison, review language, and score controls shown here. The animated panel is a walkthrough; the button opens the real API-backed product.")}
            </p>
            <ul>
              <li><ShieldCheck aria-hidden="true" /> {tx(locale, "原文优先的可追溯性", "Source-first traceability")}</li>
              <li><Eye aria-hidden="true" /> {tx(locale, "需要关注的项目保留在教师复核流程中", "Items that need attention remain visible for teacher review")}</li>
              <li><BarChart3 aria-hidden="true" /> {tx(locale, "分析说明所依据的评分数据与版本", "Analysis states which grading data and version it uses")}</li>
            </ul>
            <Link className="frontier-inline-link" to={liveDemoEntryHref}>
              {tx(locale, "进入真实工作台", "Continue in the live workspace")}
              <ChevronRight aria-hidden="true" />
            </Link>
          </div>
          <div className="frontier-workspace-window" aria-label={tx(locale, "SmarTAI 原文对照预览", "SmarTAI source comparison preview")}>
            <div className="frontier-window-bar">
              <span className="frontier-window-brand"><SmarTAIWordmark alt="" /></span>
              <span>{tx(locale, "校对学生作答", "Review Student Answers")}</span>
              <span className="frontier-demo-badge">{tx(locale, "产品导览", "Product walkthrough")}</span>
            </div>
            <div className="frontier-window-body">
              <div className="frontier-window-source">
                <div className="frontier-window-label"><FileText aria-hidden="true" /> {tx(locale, "原文件", "Original file")}</div>
                <img src="/frontier-demo/live/DEMO-002_handwritten_raw.png" alt={tx(locale, "未带评分提示的合成手写力学作答", "Synthetic handwritten mechanics answer without grading annotations")} />
              </div>
              <div className="frontier-window-divider" aria-hidden="true" />
              <div className="frontier-window-review">
                <div className="frontier-window-label"><ScanText aria-hidden="true" /> {tx(locale, "识别作答", "Recognized answer")}</div>
                <span className="frontier-attention-pill">{tx(locale, "等待教师确认", "Awaiting teacher confirmation")}</span>
                <h3>{tx(locale, "Q2 · 粗糙斜面运动", "Q2 · Motion down a rough incline")}</h3>
                <p className="frontier-recognized-answer">μ = 0.20; a = 3.20 m/s²; v = 4.38 m/s</p>
                <div className="frontier-evidence-note">
                  <strong>{tx(locale, "确认识别内容", "Confirm the recognized content")}</strong>
                  <span>{tx(locale, "这是一份手写作答。请对照左侧原件检查公式、数值和单位，再进入批改。", "This is a handwritten submission. Compare formulas, values, and units with the source before grading.")}</span>
                </div>
                <div className="frontier-score-row">
                  <span>{tx(locale, "建议得分", "Suggested score")}</span>
                  <strong>6 / 8</strong>
                </div>
                <button type="button" onClick={() => chooseStage(2)}>{tx(locale, "确认并进入批改", "Confirm and continue to grading")}</button>
              </div>
            </div>
          </div>
        </section>

        <section id="ask-smartai" className="frontier-ask-section">
          <div className="frontier-ask-heading">
            <div>
              <p className="frontier-eyebrow">Ask SmarTAI</p>
              <h2 className="frontier-title-fragments">
                {titleFragments(locale, ["批改结束，", "洞察继续。"], ["Grading ends.", "Insight continues."])}
              </h2>
            </div>
            <p>{tx(locale, "教师可以用自然语言追问当前评分结果。SmarTAI 给出可复核的回答，并可按要求返回系统支持的图表；教师应在教学决策前检查结果。", "Teachers can ask about the current grading results in natural language. SmarTAI returns a reviewable answer and, when requested, a supported chart; teachers check the result before acting on it.")}</p>
          </div>

          <div className="frontier-ask-workbench">
            <div className="frontier-ask-console">
              <div className="frontier-ask-console-title">
                <span>Ask SmarTAI</span>
                <small>{tx(locale, "产品导览 · 合成数据", "Product walkthrough · synthetic data")}</small>
              </div>
              <div className="frontier-ask-prompts" aria-label={tx(locale, "分析问题示例", "Example analysis questions")}>
                <SmarTAIMascot variant="thinking" size="sm" className="frontier-ask-mascot" />
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
                  <span>SmarTAI</span>
                  <p>{insight.answer}</p>
                  <small>{tx(locale, "依据：4 道题 · 4 份合成作答 · 当前评分版本", "Based on 4 questions · 4 synthetic submissions · current grading version")}</small>
                </div>
                <div className="frontier-teacher-query frontier-followup-query">
                  <span>{tx(locale, "教师追问", "Teacher follow-up")}</span>
                  <p>{insight.followUpQuery}</p>
                </div>
                <div className="frontier-smartai-answer frontier-followup-answer" key={`${insight.id}-follow-up`}>
                  <span>SmarTAI</span>
                  <p>{insight.followUpAnswer}</p>
                </div>
              </div>
            </div>

            <div className="frontier-generated-chart" key={`${insight.id}-chart`}>
              <div className="frontier-chart-head">
                <div>
                  <span>{tx(locale, "SmarTAI 智能问答生成", "Generated by Ask SmarTAI")}</span>
                  <h3>{insight.title}</h3>
                </div>
                <span className="frontier-chart-badge"><BarChart3 aria-hidden="true" /> {tx(locale, "当前评分数据", "Current grading data")}</span>
              </div>
              <div className="frontier-chart-request">
                <span>{tx(locale, "教师", "Teacher")}</span>
                <p>{insight.chartRequest}</p>
              </div>
              <div className="frontier-chart-generated-label">
                <span>{tx(locale, "SmarTAI 已根据当前评分结果生成下图", "Ask SmarTAI generated the chart below from the current grading results")}</span>
              </div>
              <InsightChart insight={insight} locale={locale} />
              <div className="frontier-chart-foot">
                <ShieldCheck aria-hidden="true" />
                <span>{tx(locale, "本导览图表使用上方标明的 4 份合成作答与当前评分版本。", "This walkthrough chart uses the four synthetic submissions and current grading version stated above.")}</span>
              </div>
            </div>
          </div>
        </section>

        <section id="trust" className="frontier-trust-section">
          <div>
            <p className="frontier-eyebrow">{tx(locale, "可信设计", "Trust by design")}</p>
            <h2 className="frontier-title-fragments">
              {titleFragments(locale, ["AI 提议，", "证据解释，", "教师决定。"], ["AI proposes.", "Evidence explains.", "Teachers decide."])}
            </h2>
          </div>
          <div className="frontier-trust-list">
            <article><span>01</span><h3>{tx(locale, "追溯原文", "Trace the source")}</h3><p>{tx(locale, "原文件与识别内容在整个复核过程中保持连接。", "Original files and recognized content stay connected through review.")}</p></article>
            <article><span>02</span><h3>{tx(locale, "提示教师复核", "Prompt teacher review")}</h3><p>{tx(locale, "低置信度或模型分歧会被标记，最终由教师复核判断。", "Low confidence or model disagreement is flagged for the teacher's final review.")}</p></article>
            <article><span>03</span><h3>{tx(locale, "控制发布", "Control the release")}</h3><p>{tx(locale, "教师确认最终结果前，任何分数都不会发布给学生。", "No score reaches a student until the educator confirms the final result.")}</p></article>
          </div>
        </section>

        <section className="frontier-final-cta">
          <div>
            <div className="frontier-final-brand" aria-label="SmarTAI" role="img">
              <span className="frontier-final-app-mark" aria-hidden="true"><SmarTAIAppMark finish="silver" /></span>
              <SmarTAIWordmark tone="white" alt="" className="frontier-final-wordmark" />
            </div>
            <p className="frontier-eyebrow">{tx(locale, "准备查看真实工作流？", "Ready to inspect the real workflow?")}</p>
            <h2 className="frontier-title-fragments">
              {titleFragments(locale, ["打开真实 Demo。"], ["Open the live demo."])}
            </h2>
            <p>{tx(locale, "真实工作台使用合成文件，并运行真实 API 与批改流程。", "The live workspace uses synthetic files with the real API and grading pipeline.")}</p>
          </div>
          <Link className="frontier-primary-cta frontier-primary-cta-light" to={liveDemoEntryHref}>
            {tx(locale, "进入真实 Demo", "Enter live demo")}
            <ArrowRight aria-hidden="true" />
          </Link>
        </section>
      </main>

      <footer className="frontier-footer">
        <Link className="frontier-wordmark" to="/frontier" aria-label="SmarTAI Frontier home"><SmarTAIWordmark alt="" className="frontier-wordmark-image" /></Link>
        <p>{tx(locale, "互动展示 · 合成学生数据 · 点击即可进入真实产品", "Interactive showcase · synthetic student data · live product available after entry")}</p>
      </footer>
    </div>
  );
}

function tx(locale: Locale, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

function titleFragments(locale: Locale, zh: string[], en: string[]) {
  const fragments = locale === "zh-CN" ? zh : en;
  return fragments.map((fragment, index) => (
    <span key={fragment}>
      {fragment}
      {index < fragments.length - 1 ? (locale === "zh-CN" ? "\u200B" : " ") : ""}
    </span>
  ));
}

function stageHeading(id: WalkthroughStageId, locale: Locale, fallback: string) {
  const zhFragments: Record<WalkthroughStageId, string[]> = {
    source: ["原文件，", "始终在手边。"],
    recognize: ["原件与识别结果，", "并排复核。"],
    grade: ["每一分，", "都有评分依据。"],
    analyze: ["从批改结果，", "走向班级洞察。"],
  };
  const enFragments: Record<WalkthroughStageId, string[]> = {
    source: ["Keep the source", "within reach."],
    recognize: ["Review recognition", "beside the source."],
    grade: ["Tie every point", "to its evidence."],
    analyze: ["Turn grading results", "into class insight."],
  };
  return titleFragments(locale, zhFragments[id], enFragments[id] ?? [fallback]);
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
        <div className="frontier-recognition-status">
          <span><small>{tx(locale, "完整原件", "Complete source")}</small><strong>{tx(locale, "并排打开", "Open beside text")}</strong></span>
          <span><small>{tx(locale, "结构化文本", "Structured text")}</small><strong>{tx(locale, "可修改", "Editable")}</strong></span>
          <span className="is-ready"><small>{tx(locale, "批改前", "Before grading")}</small><strong>{tx(locale, "教师确认", "Teacher confirms")}</strong></span>
        </div>
      </div>
    );
  }

  if (id === "grade") {
    return (
      <div className="frontier-grading-stack">
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
        </div>
        <div className="frontier-review-sample frontier-code-test-sample">
          <div className="frontier-sample-heading">
            <span><Code2 aria-hidden="true" /> {tx(locale, "Q4 · 代码测试", "Q4 · Code tests")}</span>
            <span>{tx(locale, "2 / 3 个隐藏测试通过", "2 / 3 hidden tests passed")}</span>
          </div>
          <div className="frontier-code-test-pills">
            <span className="is-complete"><Check aria-hidden="true" /> {tx(locale, "空输入", "Empty input")}</span>
            <span className="is-complete"><Check aria-hidden="true" /> {tx(locale, "常规数值", "Regular values")}</span>
            <span><Check aria-hidden="true" /> {tx(locale, "极端数值", "Extreme values")}</span>
          </div>
        </div>
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
        <span>{tx(locale, "Q3 的平均得分率最低。建议教师先查看该题作答，再决定是否重新讲解。", "Q3 has the lowest average attainment. Review those submissions before deciding whether to reteach it.")}</span>
      </div>
      <div className="frontier-analysis-metrics">
        <span><strong>50%</strong><small>{tx(locale, "Q3 得分率", "Q3 attainment")}</small></span>
        <span><strong>4</strong><small>{tx(locale, "份合成作答", "synthetic submissions")}</small></span>
      </div>
      <div className="frontier-analysis-provenance">
        <ShieldCheck aria-hidden="true" />
        <span>{tx(locale, "依据：题目得分与当前评分版本", "Based on question scores and the current grading version")}</span>
      </div>
    </div>
  );
}

function InsightChart({ insight, locale }: { insight: InsightStory; locale: Locale }) {
  if (insight.chart.type === "scatter") {
    return (
      <div className="frontier-chart-plot frontier-scatter-chart" role="img" aria-label={tx(locale, `散点图：${insight.title}`, `Scatter chart: ${insight.title}`)}>
        <div className="frontier-scatter-scale" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div>
        {insight.chart.points.map((point, index) => (
          <div className={`frontier-scatter-row${point.highlight ? " is-highlight" : ""}`} key={point.label}>
            <span>{point.label}</span>
            <div><i style={{ "--point-value": `${point.value}%`, "--point-delay": `${index * 90}ms` } as CSSProperties} /></div>
            <strong>{point.value}%</strong>
          </div>
        ))}
      </div>
    );
  }

  if (insight.chart.type === "box") {
    const normalizeToScale = (value: number) => `${((value - 60) / 30) * 100}%`;
    const chartStyle = {
      "--box-low": normalizeToScale(insight.chart.low),
      "--box-q1": normalizeToScale(insight.chart.q1),
      "--box-median": normalizeToScale(insight.chart.median),
      "--box-q3": normalizeToScale(insight.chart.q3),
      "--box-high": normalizeToScale(insight.chart.high),
    } as CSSProperties;
    return (
      <div className="frontier-chart-plot frontier-box-chart" role="img" aria-label={tx(locale, `箱线图：${insight.title}`, `Box chart: ${insight.title}`)} style={chartStyle}>
        <div className="frontier-box-scale" aria-hidden="true"><span>60%</span><span>70%</span><span>80%</span><span>90%</span></div>
        <div className="frontier-box-track" aria-hidden="true">
          <i className="frontier-box-whisker" />
          <i className="frontier-box-range" />
          <b className="frontier-box-median" />
          <em className="frontier-box-low">{insight.chart.low}%</em>
          <em className="frontier-box-high">{insight.chart.high}%</em>
        </div>
        <div className="frontier-box-summary">
          <span><small>{tx(locale, "中位数", "Median")}</small><strong>{insight.chart.median}%</strong></span>
          <span><small>{tx(locale, "主要区间", "Middle range")}</small><strong>{insight.chart.q1}%–{insight.chart.q3}%</strong></span>
        </div>
      </div>
    );
  }

  return (
    <div className="frontier-chart-plot frontier-donut-chart" role="img" aria-label={tx(locale, `饼图：${insight.title}`, `Pie chart: ${insight.title}`)}>
      <div className="frontier-donut" style={{ "--donut-value": `${insight.chart.value}%` } as CSSProperties} aria-hidden="true">
        <span><strong>{insight.chart.value}%</strong><small>{insight.chart.primaryLabel}</small></span>
      </div>
      <div className="frontier-donut-legend">
        <span className="is-primary"><i /> <small>{insight.chart.primaryLabel}</small><strong>{insight.chart.value}%</strong></span>
        <span><i /> <small>{insight.chart.secondaryLabel}</small><strong>{100 - insight.chart.value}%</strong></span>
      </div>
    </div>
  );
}
