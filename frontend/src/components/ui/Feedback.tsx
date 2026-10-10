import type { ReactNode } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Inbox,
  Loader2,
  SearchX,
  WifiOff,
  XCircle,
} from "lucide-react";
import { Button } from "./Button";
import { cn } from "./cn";

/** Loading spinner with an accessible label. */
export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" aria-label={label} className="inline-flex items-center gap-2 text-sm text-ink-300">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

/** Skeleton block matching content shape; render after a short delay. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-control bg-graphite-800", className ?? "h-16")}
    />
  );
}

export interface EmptyStateProps {
  icon: "inbox" | "search" | "chat" | "clock" | "offline";
  title: string;
  body: string;
  action?: ReactNode;
}

const icons = {
  inbox: Inbox,
  search: SearchX,
  chat: Clock,
  clock: Clock,
  offline: WifiOff,
};

/** First-use empty state: explains the feature and offers the action. */
export function EmptyState({ icon, title, body, action }: EmptyStateProps) {
  const Icon = icons[icon];
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-full border border-graphite-700 bg-graphite-900">
        <Icon className="h-5 w-5 text-ink-500" aria-hidden="true" />
      </span>
      <h2 className="text-base font-semibold text-ink-50">{title}</h2>
      <p className="max-w-sm text-sm leading-relaxed text-ink-500">{body}</p>
      {action}
    </div>
  );
}

export interface ErrorStateProps {
  title: string;
  body: string;
  onRetry?: () => void;
  retryLabel?: string;
}

/** Recoverable error: what happened, what to do, a retry control. */
export function ErrorState({ title, body, onRetry, retryLabel = "Retry" }: ErrorStateProps) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-full border border-signal-red/40 bg-graphite-900">
        <AlertCircle className="h-5 w-5 text-signal-red" aria-hidden="true" />
      </span>
      <h2 className="text-base font-semibold text-ink-50">{title}</h2>
      <p className="max-w-sm text-sm leading-relaxed text-ink-500">{body}</p>
      {onRetry ? (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          {retryLabel}
        </Button>
      ) : null}
    </div>
  );
}

export type StatusTone = "neutral" | "pending" | "active" | "ok" | "bad";

const toneConfig: Record<StatusTone, { icon: typeof Clock; className: string }> = {
  neutral: { icon: Clock, className: "text-ink-500" },
  pending: { icon: Clock, className: "text-signal-amber" },
  active: { icon: Loader2, className: "text-accent-400" },
  ok: { icon: CheckCircle2, className: "text-signal-green" },
  bad: { icon: XCircle, className: "text-signal-red" },
};

/** Status is always icon + text, never color alone. */
export function StatusBadge({ tone, label }: { tone: StatusTone; label: string }) {
  const config = toneConfig[tone];
  const Icon = config.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-graphite-700 bg-graphite-900 px-2.5 py-1 text-xs font-medium",
        config.className,
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}
