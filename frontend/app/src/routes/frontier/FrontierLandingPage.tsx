import {
  ArrowRight,
  BarChart3,
  Check,
  ChevronRight,
  Code2,
  Eye,
  FileCheck2,
  FileText,
  ScanText,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import "./frontier.css";

const walkthroughStages = [
  {
    id: "source",
    label: "Source",
    title: "Keep the original within reach.",
    description: "Open the complete submission beside every recognized answer instead of trusting detached text.",
    metric: "1 source · 4 answers",
  },
  {
    id: "recognize",
    label: "Recognize",
    title: "Surface uncertainty, do not hide it.",
    description: "The handwritten μ is preserved as evidence and marked for a quick teacher check.",
    metric: "82% source confidence",
  },
  {
    id: "grade",
    label: "Grade",
    title: "Tie every point to a rubric signal.",
    description: "Math reasoning, physics units, and code tests share one reviewable scoring workspace.",
    metric: "3 rubric signals found",
  },
  {
    id: "decide",
    label: "Decide",
    title: "The teacher makes the final call.",
    description: "Confirm the source, adjust the score, and retain a concise rationale before release.",
    metric: "Final score 6 / 8",
  },
] as const;

const liveDemoLoginHref = "/login?returnTo=%2Ffrontier%2Flive";

export function FrontierLandingPage() {
  const [activeStage, setActiveStage] = useState(0);
  const stage = walkthroughStages[activeStage];

  useEffect(() => {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) return;
    const timer = window.setInterval(() => {
      setActiveStage((current) => (current + 1) % walkthroughStages.length);
    }, 4200);
    return () => window.clearInterval(timer);
  }, []);

  function chooseStage(index: number) {
    setActiveStage(index);
  }

  return (
    <div className="frontier-site">
      <a className="frontier-skip" href="#frontier-main">Skip to content</a>
      <header className="frontier-nav">
        <Link className="frontier-wordmark" to="/frontier" aria-label="SmarTAI Frontier home">
          <span className="frontier-wordmark-mark" aria-hidden="true">S</span>
          <span>SmarTAI</span>
        </Link>
        <nav aria-label="Showcase navigation">
          <a href="#how-it-works">How it works</a>
          <a href="#trust">Trust by design</a>
        </nav>
        <Link className="frontier-nav-cta" to={liveDemoLoginHref}>
          Enter live demo
          <ArrowRight aria-hidden="true" />
        </Link>
      </header>

      <main id="frontier-main">
        <section className="frontier-hero">
          <div className="frontier-hero-copy">
            <div className="frontier-kicker">
              <span className="frontier-kicker-dot" aria-hidden="true" />
              AWS From Idea to Frontier · Application demo
            </div>
            <h1>From a stack of coursework to an auditable first pass.</h1>
            <p className="frontier-hero-lede">
              SmarTAI helps STEM educators review mixed mathematics, physics, and programming work without losing sight of the original submission—or control of the final grade.
            </p>
            <div className="frontier-hero-actions">
              <Link className="frontier-primary-cta" to={liveDemoLoginHref}>
                Enter the live demo
                <ArrowRight aria-hidden="true" />
              </Link>
              <a className="frontier-secondary-cta" href="#walkthrough" onClick={() => chooseStage(1)}>
                Review a handwritten answer
                <Eye aria-hidden="true" />
              </a>
            </div>
            <div className="frontier-proof-strip" aria-label="Demo boundaries">
              <span><Check aria-hidden="true" /> Synthetic student data</span>
              <span><Check aria-hidden="true" /> Real product workflow</span>
              <span><Check aria-hidden="true" /> Teacher-controlled release</span>
            </div>
          </div>

          <div className="frontier-hero-visual" data-stage={stage.id} aria-label="Interactive SmarTAI product walkthrough">
            <div className="frontier-visual-caption">
              <span>Product walkthrough</span>
              <span>Synthetic content</span>
            </div>
            <div className="frontier-document-stack" aria-hidden="true">
              <div className="frontier-page frontier-page-back" />
              <div className="frontier-page frontier-page-middle" />
              <div className="frontier-source-page">
                <img src="/frontier-demo/DEMO-002_handwritten.png" alt="" />
                <span className="frontier-source-focus" />
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
              <div className="frontier-review-sample">
                <div className="frontier-sample-heading">
                  <span>Q2 · Mechanics</span>
                  <span className={stage.id === "recognize" ? "is-warning" : ""}>{stage.metric}</span>
                </div>
                <div className="frontier-equation">a = g(sin 30° − μ cos 30°)</div>
                <div className="frontier-rubric-lines">
                  <span className="is-complete"><Check aria-hidden="true" /> force direction</span>
                  <span className={activeStage >= 2 ? "is-complete" : ""}><Check aria-hidden="true" /> acceleration</span>
                  <span className={activeStage >= 3 ? "is-complete" : ""}><Check aria-hidden="true" /> final units</span>
                </div>
              </div>
            </div>
            <div className="frontier-stage-tabs" role="tablist" aria-label="Walkthrough stages">
              {walkthroughStages.map((item, index) => (
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
            <p className="frontier-eyebrow">One reviewable workflow</p>
            <h2>Different kinds of reasoning. One place to verify them.</h2>
            <p>Every step stays connected to the source, the rubric, and the teacher decision that follows.</p>
          </div>
          <div className="frontier-process-grid">
            <ProcessCard icon={<FileText />} title="Import mixed work" copy="Start with typeset PDFs, synthetic scans, formulas, proofs, and code in the same task." label="Source intake" />
            <ProcessCard icon={<ScanText />} title="Review recognition" copy="Compare the complete original with structured questions and answers, including low-confidence regions." label="Source evidence" />
            <ProcessCard icon={<Code2 />} title="Evaluate reasoning" copy="Apply question-specific rubrics, numerical checks, and frozen code-test outcomes where appropriate." label="STEM-aware review" />
            <ProcessCard icon={<FileCheck2 />} title="Make the decision" copy="Confirm or change the score and preserve the teacher rationale before results are released." label="Teacher authority" />
          </div>
        </section>

        <section id="walkthrough" className="frontier-workspace-section">
          <div className="frontier-workspace-copy">
            <p className="frontier-eyebrow">Built from the product UI</p>
            <h2>The original file does not disappear after recognition.</h2>
            <p>
              The live demo uses the same task workspace, source comparison, review language, and score controls shown here. The animated panel is a walkthrough; the button opens the real API-backed product.
            </p>
            <ul>
              <li><ShieldCheck aria-hidden="true" /> Source-first traceability</li>
              <li><Sparkles aria-hidden="true" /> Uncertainty becomes a review queue</li>
              <li><BarChart3 aria-hidden="true" /> Class insight remains linked to questions</li>
            </ul>
            <Link className="frontier-inline-link" to={liveDemoLoginHref}>
              Continue in the live workspace
              <ChevronRight aria-hidden="true" />
            </Link>
          </div>
          <div className="frontier-workspace-window" aria-label="SmarTAI source comparison preview">
            <div className="frontier-window-bar">
              <span className="frontier-window-brand">SmarTAI</span>
              <span>Review Student Answers</span>
              <span className="frontier-demo-badge">Product walkthrough</span>
            </div>
            <div className="frontier-window-body">
              <div className="frontier-window-source">
                <div className="frontier-window-label"><FileText aria-hidden="true" /> Original file</div>
                <img src="/frontier-demo/DEMO-002_handwritten.png" alt="Synthetic handwritten mechanics answer with teacher annotations" />
              </div>
              <div className="frontier-window-divider" aria-hidden="true" />
              <div className="frontier-window-review">
                <div className="frontier-window-label"><ScanText aria-hidden="true" /> Recognized answer</div>
                <span className="frontier-attention-pill">Needs one check</span>
                <h3>Q2 · Motion down a rough incline</h3>
                <p className="frontier-recognized-answer">μ = 0.20; a = 3.20 m/s²; v = 4.38 m/s</p>
                <div className="frontier-evidence-note">
                  <strong>Why this is flagged</strong>
                  <span>The handwriting model briefly read μ as “u”. Open the source before confirming.</span>
                </div>
                <div className="frontier-score-row">
                  <span>Suggested score</span>
                  <strong>6 / 8</strong>
                </div>
                <button type="button" onClick={() => chooseStage(3)}>Confirm teacher decision</button>
              </div>
            </div>
          </div>
        </section>

        <section id="trust" className="frontier-trust-section">
          <div>
            <p className="frontier-eyebrow">Trust by design</p>
            <h2>AI proposes. Evidence explains. Teachers decide.</h2>
          </div>
          <div className="frontier-trust-list">
            <article><span>01</span><h3>Trace the source</h3><p>Original files and recognized content stay connected through review.</p></article>
            <article><span>02</span><h3>Expose uncertainty</h3><p>Low confidence and model disagreement become explicit teacher actions.</p></article>
            <article><span>03</span><h3>Control the release</h3><p>No score reaches a student until the educator confirms the final result.</p></article>
          </div>
        </section>

        <section className="frontier-final-cta">
          <div>
            <p className="frontier-eyebrow">Ready to inspect the real workflow?</p>
            <h2>Open the live SmarTAI demo.</h2>
            <p>The live workspace uses synthetic files with the real API and grading pipeline.</p>
          </div>
          <Link className="frontier-primary-cta frontier-primary-cta-light" to={liveDemoLoginHref}>
            Enter live demo
            <ArrowRight aria-hidden="true" />
          </Link>
        </section>
      </main>

      <footer className="frontier-footer">
        <Link className="frontier-wordmark" to="/frontier"><span className="frontier-wordmark-mark" aria-hidden="true">S</span><span>SmarTAI</span></Link>
        <p>Interactive showcase · synthetic student data · live product available after entry</p>
      </footer>
    </div>
  );
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
