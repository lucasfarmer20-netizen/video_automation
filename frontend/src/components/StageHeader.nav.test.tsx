/**
 * Does clicking the stage nav change the rendered stage?
 *
 * Reported from an automated browser against the deployed studio: clicking the
 * stage-nav buttons appeared to do nothing — Script content stayed on screen,
 * for Direct and for Export alike — with two candidate causes that need
 * completely different fixes:
 *
 *   (1) the click never reached React at all, or
 *   (2) `activeStage` changed and the branch still did not render.
 *
 * These tests settle (2). They wire a parent exactly as `page.tsx` does —
 * `onChange={setActiveStage}` over a branch on `activeStage === "direct"` — and
 * drive it with real React click events on the real `StageHeader`, using the
 * real `/api/stages` payload shape. If the branch renders here, then the
 * component contract is sound and any remaining failure is (1): something in
 * the page or the automation intercepting the click, not this code.
 */
import React, { useState } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import StageHeader, { Stage, StageId, STAGE_ORDER } from "./StageHeader";

afterEach(cleanup);

/**
 * The stage spine as the deployed server actually returns it, taken from a live
 * `GET /api/stages` against a project mid-run. `direct` is `complete` and
 * `roughcut` onwards are `blocked` — the exact shape under which the nav was
 * reported inert.
 */
const LIVE_STAGES: Stage[] = [
  { id: "script", name: "Script", status: "complete", blocked_reason: "", hint: "", owns: "", cta: "", cta_action: "" },
  { id: "direct", name: "Direct", status: "complete", blocked_reason: "", hint: "", owns: "", cta: "", cta_action: "" },
  { id: "generate", name: "Generate", status: "current", blocked_reason: "", hint: "", owns: "", cta: "", cta_action: "" },
  { id: "roughcut", name: "Rough Cut", status: "blocked", blocked_reason: "No visuals are ready yet.", hint: "", owns: "", cta: "", cta_action: "" },
  { id: "refine", name: "Refine", status: "blocked", blocked_reason: "There is no Draft 1 to refine yet.", hint: "", owns: "", cta: "", cta_action: "" },
  { id: "export", name: "Export", status: "blocked", blocked_reason: "There is no cut to export yet.", hint: "", owns: "", cta: "", cta_action: "" },
];

/** The wiring in page.tsx, reduced to the part under suspicion. */
function StageHost({ onChange }: { onChange?: (s: StageId) => void }) {
  const [activeStage, setActiveStage] = useState<StageId>("script");
  return (
    <div>
      <StageHeader
        stages={LIVE_STAGES}
        active={activeStage}
        onChange={(s) => {
          setActiveStage(s);
          onChange?.(s);
        }}
      />
      {activeStage === "export" ? (
        <div data-testid="stage-body">EXPORT BODY</div>
      ) : activeStage === "direct" ? (
        <div data-testid="stage-body">
          DIRECT BODY
          <button>Back to Script</button>
        </div>
      ) : (
        <div data-testid="stage-body">SCRIPT BODY</div>
      )}
    </div>
  );
}

/**
 * A stage tab, scoped to the nav.
 *
 * Deliberately not a bare role query: the Direct body also holds a "Back to
 * Script" button, and a global lookup for /Script/ would match both. Scoping to
 * the labelled nav is also what proves the click target is the tab itself.
 */
const tab = (name: string) =>
  within(screen.getByLabelText("Film stages")).getByRole("button", {
    name: new RegExp(name, "i"),
  });

describe("the stage nav, driven by real React click events", () => {
  test("clicking Direct renders the Direct branch", () => {
    const onChange = vi.fn();
    render(<StageHost onChange={onChange} />);

    // The reported starting condition: Script on screen, no "Back to Script".
    expect(screen.getByTestId("stage-body").textContent).toContain("SCRIPT BODY");
    expect(screen.queryByText("Back to Script")).toBeNull();

    fireEvent.click(tab("Direct"));

    // Cause (2) eliminated: the id the server sends reaches the branch, and the
    // branch renders. `/api/stages` returns "direct" and page.tsx:1540 tests
    // `activeStage === "direct"` — the same string, checked here end to end.
    expect(onChange).toHaveBeenCalledWith("direct");
    expect(screen.getByTestId("stage-body").textContent).toContain("DIRECT BODY");
    expect(screen.getByText("Back to Script")).toBeTruthy();
  });

  test("clicking Export renders the Export branch too", () => {
    // Reported as failing identically, which is what made it look systemic
    // rather than specific to Direct.
    render(<StageHost />);
    fireEvent.click(tab("Export"));
    expect(screen.getByTestId("stage-body").textContent).toContain("EXPORT BODY");
  });

  test("a blocked stage is still navigable — blocked is not disabled", () => {
    // Export is `blocked` in the live payload. If blocked had meant disabled,
    // the click would be swallowed and the symptom would be exactly the one
    // reported. It does not: nothing in StageHeader sets `disabled`.
    render(<StageHost />);
    const exportTab = tab("Export") as HTMLButtonElement;
    expect(exportTab.disabled).toBe(false);
    fireEvent.click(exportTab);
    expect(screen.getByTestId("stage-body").textContent).toContain("EXPORT BODY");
  });

  test("the active stage is announced, so which stage is showing is observable", () => {
    // `aria-current="step"` is set on the active tab (StageHeader.tsx:90). A
    // snapshot that exposes it can tell "the click never landed" from "the
    // click landed and the body did not follow" without guessing.
    render(<StageHost />);
    expect(tab("Script").getAttribute("aria-current")).toBe("step");
    expect(tab("Direct").getAttribute("aria-current")).toBeNull();

    fireEvent.click(tab("Direct"));

    expect(tab("Direct").getAttribute("aria-current")).toBe("step");
    expect(tab("Script").getAttribute("aria-current")).toBeNull();
  });

  test("every stage the server sends is reachable, in one session", () => {
    render(<StageHost />);
    STAGE_ORDER.forEach((id) => {
      const name = LIVE_STAGES.find((s) => s.id === id)!.name;
      fireEvent.click(tab(name));
      expect(tab(name).getAttribute("aria-current")).toBe("step");
    });
  });
});
