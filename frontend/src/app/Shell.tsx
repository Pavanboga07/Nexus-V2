import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Bot,
  CheckSquare,
  Inbox,
  LogOut,
  Menu,
  MessagesSquare,
  Settings as SettingsIcon,
  X,
} from "lucide-react";
import { useAuth } from "../lib/auth/AuthProvider";
import { useEventBus } from "./EventBus";
import { cn } from "../components/ui/cn";

const links = [
  { to: "/chats", label: "Chats", icon: MessagesSquare },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/requests", label: "Requests", icon: Inbox },
  { to: "/approvals", label: "Approvals", icon: CheckSquare },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

function ConnectionPill() {
  const { streamState } = useEventBus();
  const config: Record<string, { dot: string; label: string }> = {
    idle: { dot: "bg-ink-600", label: "Events off" },
    connecting: { dot: "bg-signal-amber", label: "Connecting" },
    connected: { dot: "bg-signal-teal", label: "Live" },
    disconnected: { dot: "bg-signal-red", label: "Reconnecting" },
  };
  const current = config[streamState];
  return (
    <span
      role="status"
      aria-label={`Event stream: ${current.label}`}
      title={`Event stream: ${current.label}`}
      className="inline-flex items-center gap-1.5 rounded-full border border-graphite-700 bg-graphite-900 px-2.5 py-1 text-xs text-ink-300"
    >
      <span aria-hidden="true" className={cn("h-2 w-2 rounded-full", current.dot)} />
      {current.label}
    </span>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const { clearToken } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2.5 px-4 pb-2 pt-5">
        <span
          aria-hidden="true"
          className="grid h-9 w-9 place-items-center rounded-control bg-accent-500 text-base font-bold text-white"
        >
          N
        </span>
        <div>
          <p className="text-[15px] font-semibold leading-tight text-ink-50">Nexus</p>
          <p className="text-xs text-ink-500">Agent workspace</p>
        </div>
      </div>
      <nav aria-label="Primary" className="flex flex-col gap-1 px-3 py-3">
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex min-h-[44px] items-center gap-3 rounded-control px-3 text-sm font-medium transition-colors",
                isActive
                  ? "bg-graphite-800 text-ink-50"
                  : "text-ink-500 hover:bg-graphite-900 hover:text-ink-100",
              )
            }
          >
            <link.icon className="h-[18px] w-[18px]" aria-hidden="true" />
            {link.label}
          </NavLink>
        ))}
      </nav>
      <div className="mt-auto flex flex-col gap-2 border-t border-graphite-800 px-4 py-4">
        <ConnectionPill />
        <button
          type="button"
          onClick={() => {
            clearToken();
            navigate("/chats", { replace: true });
            onNavigate?.();
          }}
          className="flex min-h-[44px] cursor-pointer items-center gap-3 rounded-control px-1 text-sm text-ink-500 hover:text-ink-100"
        >
          <LogOut className="h-[18px] w-[18px]" aria-hidden="true" />
          Sign out
        </button>
      </div>
    </div>
  );
}

/** Persistent desktop sidebar, drawer on mobile, connection state always
 *  visible. Focus moves to the page heading on navigation. */
export function Shell() {
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="flex min-h-dvh bg-graphite-950 text-ink-100">
      <a href="#main" className="sr-only">
        Skip to content
      </a>
      <aside className="hidden w-64 shrink-0 border-r border-graphite-800 bg-graphite-950 md:block">
        <SidebarContent />
      </aside>

      {drawerOpen ? (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div
            className="absolute inset-0 bg-black/60"
            aria-hidden="true"
            onClick={() => setDrawerOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] bg-graphite-950 shadow-2xl">
            <div className="flex justify-end p-2">
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation"
                className="grid h-11 w-11 cursor-pointer place-items-center rounded-control text-ink-300 hover:bg-graphite-800"
              >
                <X className="h-5 w-5" aria-hidden="true" />
              </button>
            </div>
            <SidebarContent onNavigate={() => setDrawerOpen(false)} />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-graphite-800 bg-graphite-950/95 px-4 backdrop-blur md:hidden">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation"
            aria-expanded={drawerOpen}
            className="grid h-11 w-11 cursor-pointer place-items-center rounded-control text-ink-100 hover:bg-graphite-800"
          >
            <Menu className="h-5 w-5" aria-hidden="true" />
          </button>
          <span className="text-[15px] font-semibold">Nexus</span>
          <span className="ml-auto md:hidden">
            <ConnectionPill />
          </span>
        </header>
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 outline-none">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
