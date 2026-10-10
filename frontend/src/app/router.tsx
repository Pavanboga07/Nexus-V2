import { Navigate, createBrowserRouter } from "react-router-dom";
import { Shell } from "./Shell";
import { ChatPage } from "../features/chat/ChatPage";
import { AgentsPage } from "../features/agents/AgentsPage";
import { RequestsPage } from "../features/requests/RequestsPage";
import { RequestDetailPage } from "../features/requests/RequestDetailPage";
import { ApprovalsPage } from "../features/approvals/ApprovalsPage";
import { SettingsPage } from "../features/settings/SettingsPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Navigate to="/chats" replace /> },
      { path: "chats", element: <ChatPage /> },
      { path: "chats/:id", element: <ChatPage /> },
      { path: "agents", element: <AgentsPage /> },
      { path: "requests", element: <RequestsPage /> },
      { path: "requests/:id", element: <RequestDetailPage /> },
      { path: "approvals", element: <ApprovalsPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <Navigate to="/chats" replace /> },
    ],
  },
]);
