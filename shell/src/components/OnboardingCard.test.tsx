import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { OnboardingCard } from "./OnboardingCard";
import type { DiscoverResponse } from "../types";
import type { BootstrapProgress } from "../useEventStream";

function discover(overrides: Partial<DiscoverResponse> = {}): DiscoverResponse {
  return {
    sources: [
      { agent: "opencode", path: "/a", available: true, count: 1200 },
      { agent: "claude_code", path: "/b", available: true, count: 3 },
      { agent: "cursor", path: "/c", available: false, reason: "not found" },
    ],
    available_count: 2,
    total_messages: 1203,
    agents: ["opencode", "claude_code"],
    ...overrides,
  };
}

describe("OnboardingCard", () => {
  it("summarizes available findings per agent", () => {
    render(
      <OnboardingCard
        discover={discover()}
        bootstrap={null}
        importing={false}
        onImport={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(
      screen.getByText(/Found 1,200 OpenCode messages and 3 Claude Code messages/),
    ).toBeInTheDocument();
  });

  it("fires onImport from the import button", () => {
    const onImport = vi.fn();
    render(
      <OnboardingCard
        discover={discover()}
        bootstrap={null}
        importing={false}
        onImport={onImport}
        onDismiss={() => {}}
      />,
    );
    screen.getByText("Import into memory").click();
    expect(onImport).toHaveBeenCalled();
  });

  it("fires onDismiss from skip", () => {
    const onDismiss = vi.fn();
    render(
      <OnboardingCard
        discover={discover()}
        bootstrap={null}
        importing={false}
        onImport={() => {}}
        onDismiss={onDismiss}
      />,
    );
    screen.getByText("Skip").click();
    expect(onDismiss).toHaveBeenCalled();
  });

  it("disables the import button and shows progress while running", () => {
    const bootstrap: BootstrapProgress = {
      phase: "progress",
      imported: 42,
      lastSummary: "ingesting opencode…",
    };
    render(
      <OnboardingCard
        discover={discover()}
        bootstrap={bootstrap}
        importing
        onImport={() => {}}
        onDismiss={() => {}}
      />,
    );
    const btn = screen.getByText("Importing…") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByText("ingesting opencode…")).toBeInTheDocument();
  });

  it("shows the completed state with imported count", () => {
    const bootstrap: BootstrapProgress = { phase: "completed", imported: 1203 };
    render(
      <OnboardingCard
        discover={discover()}
        bootstrap={bootstrap}
        importing={false}
        onImport={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText(/Imported 1,203 messages into memory/)).toBeInTheDocument();
    expect(screen.getByText("Dismiss")).toBeInTheDocument();
  });
});
