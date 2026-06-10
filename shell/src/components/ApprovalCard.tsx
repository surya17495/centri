import { useState } from "react";
import type { ApprovalCard as ApprovalCardData } from "../types";

const RISK_STYLES: Record<string, string> = {
  high: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  medium: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  low: "bg-surface-3 text-ink-dim border-surface-2",
};

export function ApprovalCard({
  card,
  onResolve,
}: {
  card: ApprovalCardData;
  onResolve: (id: string, decision: "approve" | "reject") => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const risk = RISK_STYLES[card.risk] ?? RISK_STYLES.medium;

  async function act(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await onResolve(card.approvalId, decision);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`rounded-xl border bg-surface-1 p-4 ${risk}`}>
      <div className="flex items-center gap-2">
        <span className="rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase">
          {card.risk} risk
        </span>
        <span className="text-sm font-medium text-ink">{card.label}</span>
      </div>
      {card.detail && <p className="mt-2 text-xs text-ink-dim">{card.detail}</p>}

      {card.resolved ? (
        <div className="mt-3 text-xs font-medium text-ink-dim">
          {card.resolved === "approved" ? "✓ Approved" : "✕ Rejected"}
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <button
            disabled={busy}
            onClick={() => act("approve")}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:opacity-50"
          >
            Approve
          </button>
          <button
            disabled={busy}
            onClick={() => act("reject")}
            className="rounded-lg bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-3 disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
