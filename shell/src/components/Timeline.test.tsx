import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Timeline } from "./Timeline";
import type { TimelineItem } from "../types";

const noop = async () => {};

describe("Timeline", () => {
  it("renders an empty state when there are no items", () => {
    render(<Timeline items={[]} onResolve={noop} />);
    expect(screen.getByText(/no activity yet/i)).toBeInTheDocument();
  });

  it("renders narration, task cards with streamed progress, and raw events", () => {
    const items: TimelineItem[] = [
      { kind: "narration", id: "n1", ts: "t", text: "Working on it." },
      {
        kind: "task",
        id: "task:abc",
        ts: "t",
        card: {
          taskId: "abc",
          description: "Refactor auth",
          status: "running",
          progress: [
            { ts: "t1", summary: "Reading files", percent: 10 },
            { ts: "t2", summary: "Editing module" },
          ],
          artifacts: [{ title: "auth.py", type: "diff", summary: "+12 -4" }],
          updatedAt: "t2",
        },
      },
      {
        kind: "event",
        id: "e1",
        ts: "t",
        event: { type: "hand.started", summary: "Hand spun up" },
      },
    ];

    render(<Timeline items={items} onResolve={noop} />);

    expect(screen.getByText("Working on it.")).toBeInTheDocument();
    expect(screen.getByText("Refactor auth")).toBeInTheDocument();
    // Running tasks auto-expand, so streamed progress lines are visible.
    expect(screen.getByText("Reading files")).toBeInTheDocument();
    expect(screen.getByText("Editing module")).toBeInTheDocument();
    expect(screen.getByText("auth.py")).toBeInTheDocument();
    expect(screen.getByText("hand.started")).toBeInTheDocument();
  });

  it("renders an approval card and fires resolve callbacks", async () => {
    const onResolve = vi.fn(async () => {});
    const items: TimelineItem[] = [
      {
        kind: "approval",
        id: "approval:ap1",
        ts: "t",
        card: {
          approvalId: "ap1",
          label: "Delete database",
          detail: "This will drop the users table",
          risk: "high",
        },
      },
    ];

    const { getByText } = render(<Timeline items={items} onResolve={onResolve} />);
    expect(getByText("Delete database")).toBeInTheDocument();
    expect(getByText(/high risk/i)).toBeInTheDocument();

    getByText("Approve").click();
    expect(onResolve).toHaveBeenCalledWith("ap1", "approve");

    getByText("Reject").click();
    expect(onResolve).toHaveBeenCalledWith("ap1", "reject");
  });
});
