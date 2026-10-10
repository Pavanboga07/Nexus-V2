/** Page header: title plus subtitle, one h1 per page. */
export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-5">
      <h1 className="text-2xl font-semibold tracking-tight text-ink-50 sm:text-[28px]">
        {title}
      </h1>
      {subtitle ? (
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink-500">{subtitle}</p>
      ) : null}
    </div>
  );
}
