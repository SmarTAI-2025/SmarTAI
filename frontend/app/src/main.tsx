import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { Providers } from "@/providers/Providers";
import { DashboardPage } from "@/routes/DashboardPage";
import { ExpertsPage } from "@/routes/ExpertsPage";
import { HistoryPage } from "@/routes/HistoryPage";
import { LoginPage } from "@/routes/LoginPage";
import { NewTaskPage } from "@/routes/NewTaskPage";
import { NotFoundPage } from "@/routes/NotFoundPage";
import { RegisterPage } from "@/routes/RegisterPage";
import { SettingsPage } from "@/routes/SettingsPage";
import { StudentUnavailablePage } from "@/routes/StudentUnavailablePage";
import { TaskQuestionDetailPage } from "@/routes/tasks/TaskQuestionDetailPage";
import { TaskResultsPage } from "@/routes/tasks/TaskResultsPage";
import { TaskSetupPage } from "@/routes/tasks/TaskSetupPage";
import { TaskStudentDetailPage } from "@/routes/tasks/TaskStudentDetailPage";
import { TaskUploadPage } from "@/routes/tasks/TaskUploadPage";
import "@/styles/globals.css";

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/register", element: <RegisterPage /> },
  { path: "/student", element: <StudentUnavailablePage /> },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "tasks/new", element: <NewTaskPage /> },
      { path: "tasks/:taskId", element: <Navigate to="setup" replace /> },
      { path: "tasks/:taskId/setup", element: <TaskSetupPage /> },
      { path: "tasks/:taskId/upload/:kind", element: <TaskUploadPage /> },
      { path: "tasks/:taskId/results", element: <TaskResultsPage /> },
      { path: "tasks/:taskId/results/:studentId", element: <TaskStudentDetailPage /> },
      { path: "tasks/:taskId/questions/:questionId", element: <TaskQuestionDetailPage /> },
      { path: "experts", element: <ExpertsPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <NotFoundPage /> },
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

