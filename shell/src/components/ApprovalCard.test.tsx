import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ApprovalCard } from "./ApprovalCard";
import type { ApprovalCard as ApprovalCardData } from "../types";

const base: ApprovalCardData = {
  approvalId: "ap1",
  label: "Run migration",
  detail: "Applies schema change",
  risk: "medium",
};

describe("ApprovalCard", () => {
  it("shows approve/reject controls for an unresolved approval", () => {
    render(<ApprovalCard card={base} onResolve={async () => {}} />);
    expect(screen.getByText("Run migration")).toBeInTheDocument();
    expect(screen.getByText("Approve")).toBeInTheDocument();
    expect(screen.getByText("Reject")).toBeInTheDocument();
  });

  it("invokes onResolve with the decision when approved", async () => {
    const onResolve = vi.fn(async () => {});
    render(<ApprovalCard card={base} onResolve={onResolve} />);
    screen.getByText("Approve").click();
    await waitFor(() => expect(onResolve).toHaveBeenCalledWith("ap1", "approve"));
  });

  it("renders a resolved state without action buttons", () => {
    render(<ApprovalCard card={{ ...base, resolved: "approved" }} onResolve={async () => {}} />);
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
    expect(screen.queryByText("Reject")).not.toBeInTheDocument();
  });
});
