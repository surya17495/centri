import { useState } from "react";
import type { ApprovalCard as ApprovalCardData } from "../types";

const RISK_META: Record<string, { chip: string; edge: string }> = {
  high: { chip: "bg-rose-500/15 text-rose-400", edge: "bg-rose-500/70" },
  medium: { chip: "bg-amber-500/15 text-amber-400", edge: "bg-amber-500/70" },
  low: { chip: "bg-surface-3 text-ink-dim", edge: "bg-ink-faint" },
};

export function ApprovalCard({
  card,
  onResolve,
}: {
  card: ApprovalCardData;
  onResolve: (id: string, decision: "approve" | "reject") => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const meta = RISK_META[card.risk] ?? RISK_META.medium;

  async function act(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await onResolve(card.approvalId, decision);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative overflow-hidden rounded-xl border border-line bg-surface-1 p-4 shadow-card">
      <span className={`absolute inset-y-0 left-0 w-0.5 ${meta.edge}`} aria-hidden />
      <div className="flex items-center gap-2.5">
        <span
          className={`whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${meta.chip}`}
        >
          {card.risk} risk
        </span>
        <span className="text-sm font-medium text-ink">{card.label}</span>
      </div>
      {card.detail && (
        <p className="mt-2 text-xs leading-relaxed text-ink-dim">{card.detail}</p>
      )}

      {card.resolved ? (
        <div
          className={`mt-3 inline-flex items-center gap-1.5 text-xs font-medium ${
            card.resolved === "approved" ? "text-emerald-400" : "text-ink-dim"
          }`}
        >
          {card.resolved === "approved" ? (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M20 6L9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
            </svg>
          )}
          {card.resolved === "approved" ? "Approved" : "Rejected"}
        </div>
      ) : (
        <div className="mt-3.5 flex gap-2">
          <button
            disabled={busy}
            onClick={() => act("approve")}
            className="rounded-lg bg-accent px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            Approve
          </button>
          <button
            disabled={busy}
            onClick={() => act("reject")}
            className="rounded-lg border border-line bg-transparent px-3.5 py-1.5 text-xs font-medium text-ink-dim transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
