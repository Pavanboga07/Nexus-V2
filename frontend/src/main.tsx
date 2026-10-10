import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth/AuthProvider";
import { EventProvider } from "./app/EventBus";
import { ToastProvider } from "./components/ui/Toast";
import { AuthGate } from "./app/AuthGate";
import { router } from "./app/router";
import "./styles/tokens.css";

function Gate() {
  const { token, connection } = useAuth();
  const needsSetup =
    token.length === 0 ||
    connection.kind === "unconfigured" ||
    connection.kind === "failed";
  if (needsSetup) {
    return <AuthGate />;
  }
  return <RouterProvider router={router} />;
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Missing #root element");
}

createRoot(rootElement).render(
  <StrictMode>
    <AuthProvider>
      <ToastProvider>
        <EventProvider>
          <Gate />
        </EventProvider>
      </ToastProvider>
    </AuthProvider>
  </StrictMode>,
);
