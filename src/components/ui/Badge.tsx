import type { ReactNode } from "react";

type BadgeTone = "violet" | "green" | "amber" | "red" | "gray";

const toneClasses: Record<BadgeTone, string> = {
  violet: "bg-accent/15 text-accent2",
  green: "bg-ok/15 text-ok",
  amber: "bg-warn/15 text-warn",
  red: "bg-danger/15 text-danger",
  gray: "bg-raised text-ink2",
};

export function Badge({
  tone = "gray",
  children,
}: {
  tone?: BadgeTone;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${toneClasses[tone]}`}
    >
      {children}
    </span>
  );
}
