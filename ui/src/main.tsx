import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { ApiError } from "./api/client";
import { Layout } from "./Layout";
import { AuditPage } from "./pages/AuditPage";
import { BackupsPage } from "./pages/BackupsPage";
import { ClusterDetailPage } from "./pages/ClusterDetailPage";
import { ClustersPage } from "./pages/ClustersPage";
import { OperationDetailPage } from "./pages/OperationDetailPage";
import { OperationsPage } from "./pages/OperationsPage";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Don't hammer the API on client errors (401/404); retry transient failures once.
      retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 1,
      refetchOnWindowFocus: true,
    },
  },
});

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/clusters" replace /> },
      { path: "clusters", element: <ClustersPage /> },
      { path: "clusters/:clusterId", element: <ClusterDetailPage /> },
      { path: "backups", element: <BackupsPage /> },
      { path: "operations", element: <OperationsPage /> },
      { path: "operations/:operationId", element: <OperationDetailPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "*", element: <p>Page not found.</p> },
    ],
  },
]);

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
