type ProgressTone = "violet" | "green" | "amber" | "red";

const toneClasses: Record<ProgressTone, string> = {
  violet: "accent-gradient",
  green: "bg-ok",
  amber: "bg-warn",
  red: "bg-danger",
};

export function ProgressBar({
  value,
  tone = "violet",
  className = "",
}: {
  value: number;
  tone?: ProgressTone;
  className?: string;
}) {
  const clamped = Math.min(100, Math.max(0, value));

  return (
    <div
      className={`h-2 w-full overflow-hidden rounded-full bg-raised ${className}`}
      role="progressbar"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={`h-full rounded-full transition-all ${toneClasses[tone]}`}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}
