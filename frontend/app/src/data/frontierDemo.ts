export type DemoQuestionId = "q1" | "q2" | "q3" | "q4";

export type DemoPreviewKind = "pdf" | "image";

export interface DemoQuestion {
  id: DemoQuestionId;
  label: string;
  discipline: "Calculus" | "Mechanics" | "Linear algebra" | "Programming";
  title: string;
  prompt: string;
  reference: string;
  solutionCode?: string;
  maxScore: number;
  rubric: string[];
}

export interface DemoAnswer {
  recognized: string;
  score: number;
  suggestedScore: number;
  confidence: number;
  feedback: string;
  evidence: string;
  flags: string[];
  tests?: Array<{ label: string; status: "pass" | "fail"; detail: string }>;
}

export interface DemoStudent {
  id: string;
  displayName: string;
  fileName: string;
  fileUrl: string;
  previewKind: DemoPreviewKind;
  format: string;
  recognitionConfidence: number;
  identityStatus: "confirmed" | "needs_review";
  answers: Record<DemoQuestionId, DemoAnswer>;
}

export const demoQuestions: DemoQuestion[] = [
  {
    id: "q1",
    label: "Q1",
    discipline: "Calculus",
    title: "Substitution with an exponential integral",
    prompt: "Evaluate $\\int_{0}^{1} x e^{x^2}\\,dx$. Show the substitution and the transformed bounds.",
    reference: "Let $u=x^2$ and $du=2x\\,dx$. Then $\\frac{1}{2}\\int_0^1 e^u\\,du=\\frac{e-1}{2}$.",
    maxScore: 5,
    rubric: ["Chooses u = x²", "Carries the 1/2 factor", "Transforms bounds and evaluates correctly"],
  },
  {
    id: "q2",
    label: "Q2",
    discipline: "Mechanics",
    title: "Motion down a rough incline",
    prompt: "A $2\\,\\mathrm{kg}$ block slides $3\\,\\mathrm{m}$ from rest down a $30^\\circ$ incline with $\\mu_k=0.20$. Find its acceleration and final speed. Use $g=9.81\\,\\mathrm{m\\,s^{-2}}$.",
    reference: "$a=g(\\sin 30^\\circ-\\mu_k\\cos 30^\\circ)\\approx3.20\\,\\mathrm{m\\,s^{-2}}$, then $v=\\sqrt{2as}\\approx4.38\\,\\mathrm{m\\,s^{-1}}$.",
    maxScore: 8,
    rubric: ["Resolves gravity and normal force", "Uses friction in the opposing direction", "Finds acceleration", "Finds speed with units"],
  },
  {
    id: "q3",
    label: "Q3",
    discipline: "Linear algebra",
    title: "Kernel and rank of AᵀA",
    prompt: "For any real $m\\times n$ matrix $A$, prove $\\ker(A)=\\ker(A^T A)$, then conclude $\\operatorname{rank}(A)=\\operatorname{rank}(A^T A)$. Do not assume $A$ is square or invertible.",
    reference: "$Ax=0$ implies $A^TAx=0$. Conversely, $A^TAx=0$ gives $x^TA^TAx=\\lVert Ax\\rVert^2=0$, hence $Ax=0$. Rank-nullity completes the proof.",
    maxScore: 7,
    rubric: ["Proves the forward inclusion", "Uses the norm identity for the reverse inclusion", "Applies rank-nullity without assuming A is invertible"],
  },
  {
    id: "q4",
    label: "Q4",
    discipline: "Programming",
    title: "Numerically stable softmax",
    prompt: "Implement stable_softmax(xs). Return [] for empty input and avoid overflow for values near 1000. Do not use NumPy.",
    reference: "Return [] when xs is empty; otherwise subtract max(xs), exponentiate, sum, and normalize.",
    solutionCode: "import math\n\ndef stable_softmax(xs):\n    if not xs:\n        return []\n    peak = max(xs)\n    exps = [math.exp(value - peak) for value in xs]\n    total = sum(exps)\n    return [value / total for value in exps]",
    maxScore: 10,
    rubric: ["Handles empty input", "Subtracts the maximum", "Normalizes correctly", "Passes extreme-value tests"],
  },
];

const softmaxTests = {
  partial: [
    { label: "empty input", status: "pass" as const, detail: "[] → []" },
    { label: "balanced pair", status: "pass" as const, detail: "[0, 0] → [0.5, 0.5]" },
    { label: "ordinary values", status: "pass" as const, detail: "[1, 2, 3] matches reference" },
    { label: "large values", status: "fail" as const, detail: "exp(1000) overflows" },
    { label: "wide range", status: "fail" as const, detail: "normalization returns NaN" },
  ],
  stable: [
    { label: "empty input", status: "pass" as const, detail: "[] → []" },
    { label: "balanced pair", status: "pass" as const, detail: "[0, 0] → [0.5, 0.5]" },
    { label: "ordinary values", status: "pass" as const, detail: "[1, 2, 3] matches reference" },
    { label: "large values", status: "pass" as const, detail: "[1000, 1000] → [0.5, 0.5]" },
    { label: "wide range", status: "pass" as const, detail: "[-1000, 0, 1000] ≈ [0, 0, 1]" },
  ],
};

export const demoStudents: DemoStudent[] = [
  {
    id: "DEMO-001",
    displayName: "Demo Student A",
    fileName: "DEMO-001_typeset.pdf",
    fileUrl: "/frontier-demo/DEMO-001_typeset.pdf",
    previewKind: "pdf",
    format: "LaTeX typeset PDF",
    recognitionConfidence: 0.99,
    identityStatus: "confirmed",
    answers: {
      q1: answer("Let u=x², du=2x dx. Therefore I=½[eᵘ]₀¹=(e−1)/2.", 5, 5, 0.99, "Complete substitution with correct factor and bounds.", "Page 1, lines 3–6", []),
      q2: answer("a=g(sin30°−0.2cos30°)=3.20 m/s²; v=√(2as)=4.38 m/s.", 8, 8, 0.99, "Correct force balance, direction, values, and units.", "Page 1, lines 9–15", []),
      q3: answer("Both inclusions follow from Ax=0 and xᵀAᵀAx=‖Ax‖². Rank-nullity gives equal rank.", 7, 7, 0.98, "The proof is concise and complete.", "Page 2, lines 2–9", []),
      q4: {
        ...answer("def stable_softmax(xs):\n    if not xs: return []\n    exps=[math.exp(x) for x in xs]\n    return [v/sum(exps) for v in exps]", 6, 6, 0.97, "Ordinary inputs work, but subtracting the maximum is required to avoid overflow.", "Page 2, code block", ["model disagreement", "extreme-value test failed"]),
        tests: softmaxTests.partial,
      },
    },
  },
  {
    id: "DEMO-002",
    displayName: "Demo Student B",
    fileName: "DEMO-002_handwritten.png",
    fileUrl: "/frontier-demo/DEMO-002_handwritten.png",
    previewKind: "image",
    format: "Synthetic handwritten scan",
    recognitionConfidence: 0.82,
    identityStatus: "confirmed",
    answers: {
      q1: answer("u=x², so I=[eᵘ]₀¹=e−1.", 3, 3, 0.88, "The method is appropriate, but the 1/2 from du=2x dx is missing.", "Upper third of scan", ["missing factor"]),
      q2: answer("μ=0.20; a=9.81(sin30°−μcos30°)=3.20; v=4.38 m/s.", 6, 5, 0.71, "The original clearly shows μ=0.20. OCR initially read the symbol as u; teacher verification restores one rubric point.", "Middle of scan, blue annotation", ["low OCR confidence", "teacher corrected recognition"]),
      q3: answer("If AᵀAx=0, then Ax=0 because Aᵀ cancels A.", 4, 4, 0.84, "The conclusion is right, but cancellation is not valid. Use ‖Ax‖²=0 for the reverse inclusion.", "Lower middle of scan", ["proof gap"]),
      q4: {
        ...answer("m=max(xs); exps=[exp(x-m) for x in xs]; return [e/sum(exps) for e in exps]", 8, 8, 0.86, "The numerical idea is correct. Add an explicit empty-input guard.", "Bottom code block", ["empty input not handled"]),
        tests: [
          { label: "empty input", status: "fail", detail: "max([]) raises ValueError" },
          ...softmaxTests.stable.slice(1),
        ],
      },
    },
  },
  {
    id: "DEMO-003",
    displayName: "Demo Student C",
    fileName: "DEMO-003_mixed.pdf",
    fileUrl: "/frontier-demo/DEMO-003_mixed.pdf",
    previewKind: "pdf",
    format: "Mixed typeset and annotated PDF",
    recognitionConfidence: 0.94,
    identityStatus: "confirmed",
    answers: {
      q1: answer("I=½(e−1).", 5, 5, 0.97, "Correct result with sufficient working in the source.", "Page 1, Q1", []),
      q2: answer("a=3.20 m/s² and v=4.4 m/s.", 7, 7, 0.95, "Correct method and rounded result; one unit annotation is missing in the working.", "Page 1, Q2", []),
      q3: answer("A is invertible, so AᵀA has the same kernel and rank.", 3, 3, 0.93, "The proof assumes invertibility, which is not given and excludes rectangular matrices.", "Page 2, Q3", ["invalid assumption"]),
      q4: {
        ...answer("if not xs: return []; m=max(xs); z=[exp(x-m) for x in xs]; return [v/sum(z) for v in z]", 8, 8, 0.96, "Stable and handles empty input. Improve the repeated sum and type contract.", "Page 2, code block", []),
        tests: softmaxTests.stable,
      },
    },
  },
  {
    id: "DEMO-004",
    displayName: "Identity needs review",
    fileName: "scan_004.png",
    fileUrl: "/frontier-demo/scan_004.png",
    previewKind: "image",
    format: "Synthetic unidentified scan",
    recognitionConfidence: 0.79,
    identityStatus: "needs_review",
    answers: {
      q1: answer("I=(e−1)/2.", 4, 4, 0.86, "Correct result; substitution steps are incomplete.", "Top of scan", []),
      q2: answer("a≈3.2 and v≈4.4.", 6, 6, 0.81, "Values are correct, but the free-body reasoning and units are incomplete.", "Middle of scan", ["units missing"]),
      q3: answer("", 0, 0, 0.99, "No answer is present. This is a missing response, not an OCR failure.", "No Q3 region detected", ["missing answer"]),
      q4: {
        ...answer("if not xs: return []; m=max(xs); y=[exp(x-m) for x in xs]; s=sum(y); return [v/s for v in y]", 10, 10, 0.91, "Complete stable implementation.", "Bottom of scan", []),
        tests: softmaxTests.stable,
      },
    },
  },
];

export const classInsights = [
  { questionId: "q1" as const, label: "Q1 Calculus", earned: 17, possible: 20, rate: 85 },
  { questionId: "q2" as const, label: "Q2 Mechanics", earned: 27, possible: 32, rate: 84 },
  { questionId: "q3" as const, label: "Q3 Linear algebra", earned: 14, possible: 28, rate: 50 },
  { questionId: "q4" as const, label: "Q4 Programming", earned: 32, possible: 40, rate: 80 },
];

export function demoStudentTotal(student: DemoStudent) {
  return Object.values(student.answers).reduce((total, item) => total + item.score, 0);
}

function answer(
  recognized: string,
  score: number,
  suggestedScore: number,
  confidence: number,
  feedback: string,
  evidence: string,
  flags: string[],
): DemoAnswer {
  return { recognized, score, suggestedScore, confidence, feedback, evidence, flags };
}
