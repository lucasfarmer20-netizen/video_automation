/**
 * A rate limit is never rendered as "your beat has no plan".
 *
 * What a director saw, on a beat whose coverage was planned, reviewed and on
 * disk:
 *
 *     404 / Unplanned Beat Coverage (s006)
 *     Unexpected token 'R', "Rate exceeded." is not valid JSON
 *
 * Six sequential reads of the deployed service reproduce it: three answer 200,
 * then Cloud Run's front end sheds the rest with `429` and a fourteen-byte
 * `Rate exceeded.` body. Nothing in this repository produces that string. The
 * studio parsed it as JSON anyway, the SyntaxError became the component's
 * `error` state, and the empty state that renders on `error` is headed with a
 * 404 — so a one-second load shed was presented as a specific, false, factual
 * claim about the human's work.
 *
 * The first assertion of every test here is therefore the ABSENCE of that
 * claim, not the presence of the new one. Being told your work is missing when
 * it is not is the harm; the parser leaking through was only how it got out.
 *
 * These drive the real `directorApi` over a stubbed `fetch` rather than mocking
 * the module: the defect lived in the seam between the reply and the render,
 * and a mocked API call has no seam to get wrong.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import DirectorWorkspace from "./DirectorWorkspace";

/** Phrases that tell a human their coverage is gone. */
const CLAIMS_THE_WORK_IS_MISSING = [/unplanned/i, /no coverage plan exists/i, /\b404\b/];

/** The parser's complaint, which is never a fact about a beat. */
const PARSER_COMPLAINTS = [/unexpected token/i, /is not valid json/i];

/**
 * THE DEFECT-PROVING ASSERTION. It runs first in every test that has it.
 *
 * `document.body.textContent` rather than a queried element: the claim must be
 * absent from the screen, not merely from the heading — the old panel made it
 * in a heading, and a fix that moved it into the body text would pass a
 * narrower check while showing the human exactly the same thing.
 */
function screenClaimsNothingMissing() {
  const shown = document.body.textContent || "";
  CLAIMS_THE_WORK_IS_MISSING.forEach((claim) =>
    expect(shown, `a shed request must not claim ${claim}`).not.toMatch(claim)
  );
  PARSER_COMPLAINTS.forEach((complaint) =>
    expect(shown, "the JSON parser must not speak for the server").not.toMatch(complaint)
  );
}

// --- the server, as far as this screen can tell ------------------------------

/** Exactly what Google's front end sends when it sheds a request. */
const shed = () =>
  new Response("Rate exceeded.", {
    status: 429,
    headers: { "content-type": "text/html; charset=UTF-8" },
  });

const gatewayHtml = () =>
  new Response("<html><body><h1>502 Server Error</h1></body></html>", {
    status: 502,
    headers: { "content-type": "text/html; charset=UTF-8" },
  });

const truncatedJson = () =>
  new Response('{"ok": true, "beats": [{"beat_id": "s0', {
    status: 200,
    headers: { "content-type": "application/json" },
  });

/** The app's own 404: a real statement that this beat has no plan. */
const noPlan = () =>
  new Response(JSON.stringify({ detail: "No coverage plan found for s006" }), {
    status: 404,
    headers: { "content-type": "application/json" },
  });

const FINDING = {
  id: "5a6b36245735",
  shot_id: "s006.01",
  kind: "identity_risk",
  detail: "s006.01 holds the face for 28.2s — the drift risk is the whole beat.",
};

/** A complete plan for s006, in the shape `asdict(CoveragePlan)` puts on the wire. */
function wirePlan() {
  return {
    beat_id: "s006",
    beat_duration: 28.2,
    version: 3,
    plan_id: "plan_s006",
    scene_beats: ["s006"],
    status: "draft",
    profile: "historical_docudrama",
    created_by: "planner",
    coverage: [
      {
        id: "s006.01",
        beat_id: "s006",
        tier: 1,
        purpose: "master",
        subject: "the mill yard",
        shot_size: "mw",
        angle: "front",
        camera: { move: "push", duration: 28.2, speed: 1, amount: 4 },
        identity_critical: false,
        motion_type: "parallax",
        backend: "nano2",
        prompt: "…",
        motion_prompt: "…",
        draft_variations: [],
        estimated_cost: 0.15,
      },
    ],
    warnings: [FINDING],
    warning_dispositions: {},
    beat_signature: "beatsig0000000000",
    approved_signature: "",
    approved_at: "",
    approved_by: "",
    approval_history: [],
    compiled: {},
    visual_strategy: "chiaroscuro, shadow-play reveal",
    blocking: { environment: "the mill yard at dusk" },
  };
}

const planReply = () =>
  new Response(
    JSON.stringify({
      ok: true,
      beats: [{ beat_id: "s006", beat_duration: 28.2, coverage_total: 28.2, plan: wirePlan() }],
      summary: { shots: 1, paid_shots: 0, estimated_cost: 0.15 },
      tier: "all",
    }),
    { status: 200, headers: { "content-type": "application/json" } }
  );

/** How `GET /api/director/scene` answers. Set per test, read per request. */
let sceneAnswers: (() => Response)[];
/** How `POST /api/director/warning/...` answers. */
let warningAnswer: () => Response;
let sceneReads: number;

function studioServer() {
  return vi.fn(async (url: string) => {
    if (url.includes("/api/director/scene")) {
      const answer = sceneAnswers[Math.min(sceneReads, sceneAnswers.length - 1)];
      sceneReads += 1;
      return answer();
    }
    if (url.includes("/api/director/warning/")) return warningAnswer();
    throw new Error(`unexpected request: ${url}`);
  });
}

function workspace() {
  return <DirectorWorkspace sceneId="s006" activeProjectTitle="Heney" mediaUrl={(x) => x} />;
}

/**
 * Wait out the load, generously.
 *
 * A shed read is re-issued twice with a real backoff between, which is the
 * behaviour under test — so this waits longer than the default rather than
 * neutering the delays the studio actually ships with.
 */
async function settled() {
  await waitFor(() => expect(screen.queryByText(/Querying GET/)).toBeNull(), { timeout: 8000 });
}

/** The empty state's kind, as the panel itself records it. */
function failureKind(): string | null {
  return screen.queryByTestId("coverage-load-failure")?.getAttribute("data-kind") ?? null;
}

beforeEach(() => {
  sceneAnswers = [planReply];
  warningAnswer = () =>
    new Response(JSON.stringify({ ok: true, warnings: [FINDING], warning_dispositions: {}, unresolved: 1 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  sceneReads = 0;
  vi.stubGlobal("fetch", studioServer());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a 429 on the coverage read", () => {
  test("does not tell the director their beat is unplanned", async () => {
    sceneAnswers = [shed];
    render(workspace());
    await settled();

    // THE DEFECT. Everything below is what replaced it.
    screenClaimsNothingMissing();

    expect(failureKind()).toBe("rate_limited");
    expect(screen.getByTestId("coverage-load-failure").textContent).toMatch(/rate-limited/i);
    expect(document.body.textContent).toMatch(/says nothing about the beat's coverage/i);
  }, 20000);

  test("does not offer to plan over coverage it merely could not read", async () => {
    // The sharper half. The unplanned panel's call to action is
    // POST /api/director/plan, which writes new coverage over whatever is there
    // — so the false claim came with a button that would have made it true.
    sceneAnswers = [shed];
    render(workspace());
    await settled();

    expect(
      screen.queryByTestId("plan-unplanned-beat"),
      "planning over an unread beat destroys the plan the studio failed to show"
    ).toBeNull();
    expect(screen.getByTestId("retry-coverage-load")).toBeTruthy();
  }, 20000);

  test("quotes what the server said, without letting the quote be the finding", async () => {
    sceneAnswers = [shed];
    render(workspace());
    await settled();

    screenClaimsNothingMissing();
    expect(document.body.textContent).toContain("Rate exceeded.");
  }, 20000);

  test("re-reading brings the plan back, because the plan was always there", async () => {
    sceneAnswers = [shed, shed, shed, planReply];
    render(workspace());
    await settled();
    expect(failureKind()).toBe("rate_limited");

    fireEvent.click(screen.getByTestId("retry-coverage-load"));
    await settled();

    expect(screen.queryByTestId("coverage-load-failure")).toBeNull();
    expect(document.body.textContent).toContain("s006.01");
  }, 20000);
});

describe("a 5xx HTML page on the coverage read", () => {
  test("reads as a failed request, not as a missing plan", async () => {
    sceneAnswers = [gatewayHtml];
    render(workspace());
    await settled();

    screenClaimsNothingMissing();

    expect(failureKind()).toBe("unreachable");
    expect(screen.queryByTestId("plan-unplanned-beat")).toBeNull();
  }, 20000);
});

describe("a 200 whose body will not parse", () => {
  test("reads as a reply that could not be read, not as a missing plan", async () => {
    sceneAnswers = [truncatedJson];
    render(workspace());
    await settled();

    screenClaimsNothingMissing();

    expect(failureKind()).toBe("unreachable");
    expect(screen.queryByTestId("plan-unplanned-beat")).toBeNull();
  }, 20000);
});

describe("a real 404 still reads as not found", () => {
  test("the beat that genuinely has no plan says so, and offers to plan it", async () => {
    // The opposite direction, and it is the one a careless fix breaks: swallow
    // every failure into "the studio could not answer" and a genuinely
    // unplanned beat becomes unactionable, with the control that would plan it
    // hidden behind a transport excuse.
    sceneAnswers = [noPlan];
    render(workspace());
    await settled();

    expect(failureKind()).toBe("missing");
    expect(document.body.textContent).toMatch(/unplanned beat coverage/i);
    expect(screen.getByTestId("plan-unplanned-beat")).toBeTruthy();
    expect(screen.queryByTestId("retry-coverage-load")).toBeNull();
  }, 20000);
});

describe("a 429 on recording a decision — the call that produced the screenshot", () => {
  test("does not replace the workspace with a claim that the beat is unplanned", async () => {
    // `decideDirectorWarning` did a bare `res.json()`, its SyntaxError reached
    // `setError`, and `setError` feeds the same empty state — so a shed write
    // wiped a loaded plan off the screen and headed the result with a 404.
    warningAnswer = shed;
    render(workspace());
    await settled();

    fireEvent.click(screen.getByText("Review Problems"));
    fireEvent.click(screen.getByText("Mark resolved"));

    await waitFor(() => expect(screen.queryByTestId("coverage-load-failure")).toBeTruthy(), {
      timeout: 8000,
    });

    screenClaimsNothingMissing();

    expect(failureKind()).toBe("rate_limited");
    expect(screen.queryByTestId("plan-unplanned-beat")).toBeNull();
  }, 20000);

  test("nor does an ordinary refusal of that write, which is also not a 404", async () => {
    // The plan is loaded and in state throughout — `setError` merely stops it
    // being rendered. So no refusal of this write, transport or otherwise, is
    // evidence that the beat is unplanned, and the panel must not say it is.
    warningAnswer = () =>
      new Response(JSON.stringify({ ok: false, error: "unknown warning id w_3f2a" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      });
    render(workspace());
    await settled();

    fireEvent.click(screen.getByText("Review Problems"));
    fireEvent.click(screen.getByText("Mark resolved"));

    await waitFor(() => expect(screen.queryByTestId("coverage-load-failure")).toBeTruthy(), {
      timeout: 8000,
    });

    screenClaimsNothingMissing();

    expect(document.body.textContent).toContain("unknown warning id w_3f2a");
    expect(screen.queryByTestId("plan-unplanned-beat")).toBeNull();
  }, 20000);
});
