import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "./cn";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
type ButtonSize = "sm" | "md" | "icon";

const variants: Record<ButtonVariant, string> = {
  primary:
    "bg-accent-500 text-white hover:bg-accent-400 disabled:bg-graphite-700 disabled:text-ink-600",
  secondary:
    "border border-graphite-700 bg-graphite-800 text-ink-100 hover:border-ink-600 hover:bg-graphite-850 disabled:text-ink-600",
  danger: "bg-red-500/90 text-white hover:bg-red-500 disabled:bg-graphite-700 disabled:text-ink-600",
  ghost: "text-ink-300 hover:bg-graphite-800 hover:text-ink-50 disabled:text-ink-600",
};

const sizes: Record<ButtonSize, string> = {
  sm: "h-8 min-h-[32px] px-3 text-[13px]",
  md: "h-10 min-h-[40px] px-4 text-sm",
  icon: "h-9 w-9 min-h-[36px] min-w-[36px]",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  busy?: boolean;
  children: ReactNode;
}

/** Explicit button variants. The primary action is solid electric blue;
 *  danger is a red fill used only beside confirmations. */
export function Button({
  variant = "secondary",
  size = "md",
  busy = false,
  className,
  disabled,
  children,
  type,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type ?? "button"}
      disabled={disabled ?? busy}
      aria-busy={busy}
      className={cn(
        "inline-flex cursor-pointer items-center justify-center gap-2 rounded-control font-medium transition-colors",
        "disabled:cursor-not-allowed",
        variants[variant],
        sizes[size],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
