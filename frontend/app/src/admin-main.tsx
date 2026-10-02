import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { RequireAdminSession } from "@/components/auth/RequireAdminSession";
import { AdminShell } from "@/components/layout/AdminShell";
import { Providers } from "@/providers/Providers";
import { AdminOverviewPage } from "@/routes/admin/AdminOverviewPage";
import { AdminUsersPage } from "@/routes/admin/AdminUsersPage";
import { AdminInvitesPage } from "@/routes/admin/AdminInvitesPage";
import { AdminAuditPage } from "@/routes/admin/AdminAuditPage";
import { LoginPage } from "@/routes/LoginPage";
import "@/styles/globals.css";

const router = createBrowserRouter([{
  path: "/login", element: <LoginPage />,
}, {
  path: "/admin",
  element: <RequireAdminSession><AdminShell /></RequireAdminSession>,
  children: [
    { index: true, element: <AdminOverviewPage /> },
    { path: "users", element: <AdminUsersPage /> },
    { path: "invites", element: <AdminInvitesPage /> },
    { path: "audit", element: <AdminAuditPage /> },
  ],
}]);

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><Providers><RouterProvider router={router} /></Providers></React.StrictMode>);
