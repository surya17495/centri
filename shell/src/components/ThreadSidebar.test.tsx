import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThreadSidebar } from "./ThreadSidebar";
import type { Thread } from "../types";

const threads: Thread[] = [
  { id: "th-A", title: "Refactor auth" },
  { id: "th-B", title: "Bench scenarios" },
];

describe("ThreadSidebar", () => {
  it("lists threads and marks the active one", () => {
    render(
      <ThreadSidebar threads={threads} activeThreadId="th-B" onSelect={() => {}} onNew={() => {}} />,
    );
    expect(screen.getByText("Refactor auth")).toBeInTheDocument();
    const active = screen.getByText("Bench scenarios");
    expect(active.getAttribute("aria-current")).toBe("true");
  });

  it("fires onSelect when a thread is clicked", () => {
    const onSelect = vi.fn();
    render(
      <ThreadSidebar threads={threads} activeThreadId={null} onSelect={onSelect} onNew={() => {}} />,
    );
    screen.getByText("Refactor auth").click();
    expect(onSelect).toHaveBeenCalledWith("th-A");
  });

  it("fires onNew from the new-thread control", () => {
    const onNew = vi.fn();
    render(
      <ThreadSidebar threads={[]} activeThreadId={null} onSelect={() => {}} onNew={onNew} />,
    );
    expect(screen.getByText(/no threads yet/i)).toBeInTheDocument();
    screen.getByLabelText("New thread").click();
    expect(onNew).toHaveBeenCalled();
  });
});
