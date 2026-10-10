import {
  cloneElement,
  isValidElement,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { cn } from "./cn";

const controlClass =
  "w-full rounded-control border border-graphite-700 bg-graphite-900 px-3 py-2 text-sm text-ink-50 placeholder:text-ink-600 hover:border-ink-600 aria-[invalid=true]:border-signal-red";

export interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  optional?: boolean;
  children: ReactNode;
}

/** Label + hint + error wiring per building-accessible-interfaces:
 *  every input labelled, errors linked via aria-describedby. */
export function Field({ label, hint, error, optional = false, children }: FieldProps) {
  const id = useId();
  const inputId = `nexus-input-${id}`;
  const errorId = `nexus-error-${id}`;
  const hintElementId = `nexus-hint-${id}`;
  const describedBy = [hint ? hintElementId : null, error ? errorId : null]
    .filter((part): part is string => part !== null)
    .join(" ");

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-[13px] font-medium text-ink-300">
        {label}
        {optional ? (
          <span className="ml-1.5 font-normal text-ink-500">(optional)</span>
        ) : null}
      </label>
      {hint ? (
        <p id={hintElementId} className="text-xs text-ink-500">
          {hint}
        </p>
      ) : null}
      {isValidElement(children)
        ? cloneElement(children, {
            id: inputId,
            "aria-invalid": error ? true : undefined,
            "aria-describedby": describedBy.length > 0 ? describedBy : undefined,
          } as Record<string, unknown>)
        : children}
      {error ? (
        <p id={errorId} role="alert" className="text-xs text-signal-red">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(controlClass, "h-10", props.className)} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(controlClass, "min-h-[96px] resize-y", props.className)} />;
}

export function SelectControl(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn(controlClass, "h-10", props.className)} />;
}
