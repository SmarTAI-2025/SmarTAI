import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { RequireTeacherSession } from "@/components/auth/RequireTeacherSession";
import { AppShell } from "@/components/layout/AppShell";
import { useI18n } from "@/i18n/I18nProvider";
import { Providers } from "@/providers/Providers";
import { StudentUnavailablePage } from "@/routes/StudentUnavailablePage";
import "@/styles/globals.css";

const DashboardPage = React.lazy(() =>
  import("@/routes/DashboardPage").then((module) => ({ default: module.DashboardPage })),
);
const ExpertsPage = React.lazy(() => import("@/routes/ExpertsPage").then((module) => ({ default: module.ExpertsPage })));
const HistoryPage = React.lazy(() => import("@/routes/HistoryPage").then((module) => ({ default: module.HistoryPage })));
const KnowledgeBasePage = React.lazy(() =>
  import("@/routes/KnowledgeBasePage").then((module) => ({ default: module.KnowledgeBasePage })),
);
const LoginPage = React.lazy(() => import("@/routes/LoginPage").then((module) => ({ default: module.LoginPage })));
const NewTaskPage = React.lazy(() => import("@/routes/NewTaskPage").then((module) => ({ default: module.NewTaskPage })));
const NotFoundPage = React.lazy(() =>
  import("@/routes/NotFoundPage").then((module) => ({ default: module.NotFoundPage })),
);
const RegisterPage = React.lazy(() => import("@/routes/RegisterPage").then((module) => ({ default: module.RegisterPage })));
const SettingsPage = React.lazy(() =>
  import("@/routes/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);
const FinalResultsWorkspacePage = React.lazy(() =>
  import("@/routes/tasks/FinalResultsWorkspacePage").then((module) => ({ default: module.FinalResultsWorkspacePage })),
);
const TaskEntryRedirect = React.lazy(() =>
  import("@/routes/tasks/TaskEntryRedirect").then((module) => ({ default: module.TaskEntryRedirect })),
);
const AddProblemsPage = React.lazy(() =>
  import("@/routes/tasks/AddProblemsPage").then((module) => ({ default: module.AddProblemsPage })),
);
const AddSubmissionsPage = React.lazy(() =>
  import("@/routes/tasks/AddSubmissionsPage").then((module) => ({ default: module.AddSubmissionsPage })),
);
const SubmissionRecognitionProgressPage = React.lazy(() =>
  import("@/routes/tasks/SubmissionRecognitionProgressPage").then((module) => ({ default: module.SubmissionRecognitionProgressPage })),
);
const SubmissionReviewOverviewPage = React.lazy(() =>
  import("@/routes/tasks/SubmissionReviewOverviewPage").then((module) => ({ default: module.SubmissionReviewOverviewPage })),
);
const StudentAnswerReviewPage = React.lazy(() =>
  import("@/routes/tasks/StudentAnswerReviewPage").then((module) => ({ default: module.StudentAnswerReviewPage })),
);
const ProblemRecognitionProgressPage = React.lazy(() =>
  import("@/routes/tasks/ProblemRecognitionProgressPage").then((module) => ({ default: module.ProblemRecognitionProgressPage })),
);
const QuestionPreparationOverviewPage = React.lazy(() =>
  import("@/routes/tasks/QuestionPreparationOverviewPage").then((module) => ({ default: module.QuestionPreparationOverviewPage })),
);
const QuestionPreparationDetailPage = React.lazy(() =>
  import("@/routes/tasks/QuestionPreparationDetailPage").then((module) => ({ default: module.QuestionPreparationDetailPage })),
);
const QuestionMaterialImportPage = React.lazy(() =>
  import("@/routes/tasks/QuestionMaterialImportPage").then((module) => ({ default: module.QuestionMaterialImportPage })),
);
const QuestionMaterialImportProgressPage = React.lazy(() =>
  import("@/routes/tasks/QuestionMaterialImportProgressPage").then((module) => ({ default: module.QuestionMaterialImportProgressPage })),
);
const QuestionMaterialImportReviewPage = React.lazy(() =>
  import("@/routes/tasks/QuestionMaterialImportReviewPage").then((module) => ({ default: module.QuestionMaterialImportReviewPage })),
);
const QuestionAICompletionPage = React.lazy(() =>
  import("@/routes/tasks/QuestionAICompletionPage").then((module) => ({ default: module.QuestionAICompletionPage })),
);
const QuestionAICompletionProgressPage = React.lazy(() =>
  import("@/routes/tasks/QuestionAICompletionProgressPage").then((module) => ({ default: module.QuestionAICompletionProgressPage })),
);
const GradingSetupPage = React.lazy(() =>
  import("@/routes/tasks/GradingSetupPage").then((module) => ({ default: module.GradingSetupPage })),
);
const GradingPreflightPage = React.lazy(() =>
  import("@/routes/tasks/GradingPreflightPage").then((module) => ({ default: module.GradingPreflightPage })),
);
const GradingProgressPage = React.lazy(() =>
  import("@/routes/tasks/GradingProgressPage").then((module) => ({ default: module.GradingProgressPage })),
);
const ReviewOverviewPage = React.lazy(() =>
  import("@/routes/tasks/ReviewOverviewPage").then((module) => ({ default: module.ReviewOverviewPage })),
);
const ReviewDetailPage = React.lazy(() =>
  import("@/routes/tasks/ReviewDetailPage").then((module) => ({ default: module.ReviewDetailPage })),
);

function RouteFallback() {
  const { t } = useI18n();

  return (
    <div className="flex min-h-[50vh] items-center justify-center text-sm font-medium text-slate-500">
      {t("loading")}
    </div>
  );
}

function routeElement(element: React.ReactNode) {
  return <React.Suspense fallback={<RouteFallback />}>{element}</React.Suspense>;
}

const router = createBrowserRouter([
  { path: "/login", element: routeElement(<LoginPage />) },
  { path: "/register", element: routeElement(<RegisterPage />) },
  { path: "/student", element: <StudentUnavailablePage /> },
  {
    path: "/",
    element: (
      <RequireTeacherSession>
        <AppShell />
      </RequireTeacherSession>
    ),
    children: [
      { index: true, element: routeElement(<DashboardPage />) },
      { path: "dashboard", element: <Navigate to="/" replace /> },
      { path: "history", element: routeElement(<HistoryPage />) },
      { path: "knowledge-base", element: routeElement(<KnowledgeBasePage />) },
      { path: "tasks/new", element: routeElement(<NewTaskPage />) },
      { path: "tasks/:taskId/edit", element: routeElement(<NewTaskPage />) },
      { path: "tasks/:taskId", element: routeElement(<TaskEntryRedirect />) },
      { path: "tasks/:taskId/setup", element: routeElement(<TaskEntryRedirect />) },
      { path: "tasks/:taskId/upload/problems", element: routeElement(<AddProblemsPage />) },
      { path: "tasks/:taskId/submissions/upload", element: routeElement(<AddSubmissionsPage />) },
      { path: "tasks/:taskId/submissions/progress", element: routeElement(<SubmissionRecognitionProgressPage />) },
      { path: "tasks/:taskId/submissions", element: routeElement(<SubmissionReviewOverviewPage />) },
      { path: "tasks/:taskId/students/:studentId", element: routeElement(<StudentAnswerReviewPage />) },
      { path: "tasks/:taskId/upload/submissions", element: routeElement(<TaskEntryRedirect />) },
      { path: "tasks/:taskId/upload/:kind", element: routeElement(<TaskEntryRedirect />) },
      { path: "tasks/:taskId/problems/progress", element: routeElement(<ProblemRecognitionProgressPage />) },
      { path: "tasks/:taskId/questions", element: routeElement(<QuestionPreparationOverviewPage />) },
      { path: "tasks/:taskId/questions/import", element: routeElement(<QuestionMaterialImportPage />) },
      { path: "tasks/:taskId/questions/import/progress/:jobId", element: routeElement(<QuestionMaterialImportProgressPage />) },
      { path: "tasks/:taskId/questions/import/review/:jobId", element: routeElement(<QuestionMaterialImportReviewPage />) },
      { path: "tasks/:taskId/questions/ai-complete", element: routeElement(<QuestionAICompletionPage />) },
      { path: "tasks/:taskId/questions/ai-complete/progress/:jobId", element: routeElement(<QuestionAICompletionProgressPage />) },
      { path: "tasks/:taskId/questions/:questionId", element: <Navigate to="content" replace /> },
      { path: "tasks/:taskId/questions/:questionId/:section", element: routeElement(<QuestionPreparationDetailPage />) },
      { path: "tasks/:taskId/grading-setup", element: routeElement(<GradingSetupPage />) },
      { path: "tasks/:taskId/grading/preflight", element: routeElement(<GradingPreflightPage />) },
      { path: "tasks/:taskId/grading/progress", element: routeElement(<GradingProgressPage />) },
      { path: "tasks/:taskId/review", element: routeElement(<ReviewOverviewPage />) },
      { path: "tasks/:taskId/review/:studentId/:questionId", element: routeElement(<ReviewDetailPage />) },
      { path: "tasks/:taskId/results", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/questions", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/students", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/visualizations", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/reports", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/students/:studentId", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "tasks/:taskId/results/questions/:questionId", element: routeElement(<FinalResultsWorkspacePage />) },
      { path: "settings/account", element: routeElement(<SettingsPage />) },
      { path: "settings/byok", element: routeElement(<ExpertsPage />) },
      { path: "experts", element: <Navigate to="/settings/byok" replace /> },
      { path: "settings", element: <Navigate to="/settings/account" replace /> },
      { path: "*", element: routeElement(<NotFoundPage />) },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Providers>
      <RouterProvider router={router} />
    </Providers>
  </React.StrictMode>,
);
