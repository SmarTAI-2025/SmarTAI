import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { RequireTeacherSession } from "@/components/auth/RequireTeacherSession";
import { AppShell } from "@/components/layout/AppShell";
import { Providers } from "@/providers/Providers";
import { StudentUnavailablePage } from "@/routes/StudentUnavailablePage";
import "@/styles/globals.css";

const DashboardPage = React.lazy(() =>
  import("@/routes/DashboardPage").then((module) => ({ default: module.DashboardPage })),
);
const ExpertsPage = React.lazy(() => import("@/routes/ExpertsPage").then((module) => ({ default: module.ExpertsPage })));
const HistoryPage = React.lazy(() => import("@/routes/HistoryPage").then((module) => ({ default: module.HistoryPage })));
const LoginPage = React.lazy(() => import("@/routes/LoginPage").then((module) => ({ default: module.LoginPage })));
const NewTaskPage = React.lazy(() => import("@/routes/NewTaskPage").then((module) => ({ default: module.NewTaskPage })));
const NotFoundPage = React.lazy(() =>
  import("@/routes/NotFoundPage").then((module) => ({ default: module.NotFoundPage })),
);
const RegisterPage = React.lazy(() => import("@/routes/RegisterPage").then((module) => ({ default: module.RegisterPage })));
const SettingsPage = React.lazy(() =>
  import("@/routes/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);
const TaskQuestionDetailPage = React.lazy(() =>
  import("@/routes/tasks/TaskQuestionDetailPage").then((module) => ({ default: module.TaskQuestionDetailPage })),
);
const TaskResultsPage = React.lazy(() =>
  import("@/routes/tasks/TaskResultsPage").then((module) => ({ default: module.TaskResultsPage })),
);
const TaskSetupPage = React.lazy(() =>
  import("@/routes/tasks/TaskSetupPage").then((module) => ({ default: module.TaskSetupPage })),
);
const TaskStudentDetailPage = React.lazy(() =>
  import("@/routes/tasks/TaskStudentDetailPage").then((module) => ({ default: module.TaskStudentDetailPage })),
);
const TaskUploadPage = React.lazy(() =>
  import("@/routes/tasks/TaskUploadPage").then((module) => ({ default: module.TaskUploadPage })),
);

function RouteFallback() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center text-sm font-medium text-slate-500">
      Loading...
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
      { path: "history", element: routeElement(<HistoryPage />) },
      { path: "tasks/new", element: routeElement(<NewTaskPage />) },
      { path: "tasks/:taskId", element: <Navigate to="setup" replace /> },
      { path: "tasks/:taskId/setup", element: routeElement(<TaskSetupPage />) },
      { path: "tasks/:taskId/upload/:kind", element: routeElement(<TaskUploadPage />) },
      { path: "tasks/:taskId/results", element: routeElement(<TaskResultsPage />) },
      { path: "tasks/:taskId/results/:studentId", element: routeElement(<TaskStudentDetailPage />) },
      { path: "tasks/:taskId/questions/:questionId", element: routeElement(<TaskQuestionDetailPage />) },
      { path: "experts", element: routeElement(<ExpertsPage />) },
      { path: "settings", element: routeElement(<SettingsPage />) },
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
