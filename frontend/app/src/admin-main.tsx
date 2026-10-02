import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import { RequireAdminSession } from "@/components/auth/RequireAdminSession";
import { AdminShell } from "@/components/layout/AdminShell";
import { Providers } from "@/providers/Providers";
import { AdminMonitoringPage } from "@/routes/admin/AdminMonitoringPage";
import { AdminAnalyticsPage } from "@/routes/admin/AdminAnalyticsPage";
import { AdminMaintenancePage } from "@/routes/admin/AdminMaintenancePage";
import { AdminOverviewPage } from "@/routes/admin/AdminOverviewPage";
import { AdminUsersPage } from "@/routes/admin/AdminUsersPage";
import { AdminUserDetailPage } from "@/routes/admin/AdminUserDetailPage";
import { AdminClosurePage } from "@/routes/admin/AdminClosurePage";
import { AdminAccountPage } from "@/routes/admin/AdminAccountPage";
import { ForgotPasswordPage } from "@/routes/ForgotPasswordPage";
import { ResetPasswordPage } from "@/routes/ResetPasswordPage";
import { AdminBusinessConfigPage } from "@/routes/admin/AdminBusinessConfigPage";
import { AdminAuditPage } from "@/routes/admin/AdminAuditPage";
import { LoginPage } from "@/routes/LoginPage";
import "@/styles/globals.css";

const router = createBrowserRouter([{
  path: "/login", element: <LoginPage admin />,
}, { path: "/", element: <Navigate to="/admin" replace />
}, { path: "/forgot-password", element: <ForgotPasswordPage />
}, { path: "/reset-password", element: <ResetPasswordPage />
}, {
  path: "/admin",
  element: <RequireAdminSession><AdminShell /></RequireAdminSession>,
  children: [
    { index: true, element: <AdminOverviewPage /> },
    { path: "monitoring", element: <AdminMonitoringPage /> },
    { path: "analytics", element: <AdminAnalyticsPage /> },
    { path: "maintenance", element: <AdminMaintenancePage /> },
    { path: "business-config", element: <AdminBusinessConfigPage /> },
    { path: "users", element: <AdminUsersPage /> },
    { path: "users/:userId", element: <AdminUserDetailPage /> },
    { path: "account", element: <AdminAccountPage /> },
    { path: "closures/:closureId", element: <AdminClosurePage /> },
    { path: "audit", element: <AdminAuditPage /> },
  ],
}]);

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><Providers><RouterProvider router={router} /></Providers></React.StrictMode>);
