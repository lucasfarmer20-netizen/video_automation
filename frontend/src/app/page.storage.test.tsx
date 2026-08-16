/**
 * The storage gate, on the screen.
 *
 * The backend now refuses rather than answering 200 over local disk it cannot
 * vouch for (`503`, `detail.storage_gate === "unavailable"` — see
 * `storage_gate_unavailable` in backend/main.py). That refusal has to LAND
 * somewhere, and where it landed before was the worst possible place:
 *
 *   `fetchActiveProject` reads `data.ok`, finds it absent, installs nothing, and
 *   the page falls through to `!activeProject` — the "No Active Project Loaded"
 *   screen, whose single call to action is **Initialize Project Workspace**.
 *
 * So during an outage the studio told the user their project was not there and
 * offered to create a new one over it. Failing closed without this is not a
 * smaller defect than the one it replaces; it is a different one.
 *
 * The first assertion in the headline test is therefore the absence of that
 * button, not the presence of the new screen: it is the assertion that fails
 * under the regression, and it fails on a claim rather than on a timeout.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

/** How /api/project/active behaves. Tests set this before (or during) a render. */
let activeOutcome:
  | "ok" | "storage-gate" | "gate-no-message" | "plain-503" | "not-found" = "ok";

const PROJECT_ID = "leshy";

/** The message the server actually sends, which the screen must show verbatim. */
const GATE_MESSAGE =
  "could not read project 'leshy' from the durable store: ServiceUnavailable: " +
  "503 failed to connect. Refusing to answer from local disk as though it were " +
  "authoritative -- on Cloud Run that copy may be the image's ephemeral one.";

const beat = (id: string) => ({
  scene_id: id, narration: "", motion_type: "parallax",
  camera: { move: "static", duration: 4 },
  draft_image: "assets/s001/var_A.png", draft_variations: ["assets/s001/var_A.png"],
  chosen_variation: 0,
});

function payloadFor(url: string) {
  if (url.startsWith("/api/projects")) {
    return {
      ok: true,
      projects: [{
        name: "Leshy", rel: "/w/leshy/storyboard_manifest.json", project_id: PROJECT_ID,
        rel_display: "Leshy", active: true, channel: "bestiary",
      }],
    };
  }
  if (url.startsWith("/api/project/active")) {
    return {
      ok: true,
      project_id: PROJECT_ID,
      project: {
        id: PROJECT_ID, title: "Leshy", channel: "bestiary", name: "Leshy",
        storyboard_approved: true, script_locked: true, shots: [beat("s001")],
      },
    };
  }
  if (url.startsWith("/api/stages")) {
    return {
      ok: true,
      stages: ["script", "direct", "generate", "roughcut", "refine", "export"].map((id) => ({
        id, name: id, status: "available", blocked_reason: "", hint: "", cta: "",
        cta_action: "", owns: "",
      })),
    };
  }
  if (url.startsWith("/api/timeline/slots")) {
    return { ok: true, slots: [], coverage: { slots: 0, ready: 0, placeholders: 0, runtime: 0, summary: "" } };
  }
  if (url.startsWith("/api/assemble/status")) return { ok: true, jobs: {} };
  if (url.startsWith("/api/metadata")) return { ok: true, metadata: null };
  if (url.startsWith("/api/audio/peaks")) return { ok: true, peaks: {} };
  return { ok: true };
}

const reply = (status: number, body: any) => ({
  ok: status < 400,
  status,
  headers: new Headers({ "X-Project-Id": PROJECT_ID }),
  json: async () => body,
  text: async () => JSON.stringify(body),
}) as unknown as Response;

beforeEach(() => {
  activeOutcome = "ok";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo) => {
    const url = String(input).replace("http://localhost:5000", "");

    if (url.startsWith("/api/project/active")) {
      if (activeOutcome === "storage-gate") {
        // Exactly what FastAPI serialises for
        // `HTTPException(503, detail={...})`.
        return reply(503, {
          detail: { error: GATE_MESSAGE, storage_gate: "unavailable", project_id: PROJECT_ID },
        });
      }
      if (activeOutcome === "gate-no-message") {
        // The gate is named but the prose is missing — an older backend, a
        // trimmed proxy body, a future refusal that forgets the sentence.
        return reply(503, {
          detail: { storage_gate: "unavailable", project_id: PROJECT_ID },
        });
      }
      if (activeOutcome === "plain-503") {
        // A 503 that says nothing about the durable store — a proxy, a cold
        // start, a load balancer. The status alone must not be read as the gate.
        return reply(503, { ok: false, error: "upstream unavailable" });
      }
      if (activeOutcome === "not-found") {
        return reply(404, { ok: false, error: "unknown project" });
      }
    }

    return reply(200, payloadFor(url));
  }));
  vi.stubGlobal("alert", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const settled = () =>
  waitFor(() => expect(screen.queryByText(/Loading Workspace/i)).toBeNull());

const createButton = () =>
  screen.queryByRole("button", { name: /initialize project workspace/i });

describe("an unavailable durable store is stated, not turned into a missing project", () => {
  test("the studio never offers to create a project over one it cannot reach", async () => {
    activeOutcome = "storage-gate";
    render(<Page />);
    await settled();

    // THE DEFECT, first. Without the storage-gate branch the page falls through
    // to "No Active Project Loaded" and this button is on screen, inviting the
    // user to seed a fresh manifest over a film that exists and is merely
    // unreachable.
    expect(createButton()).toBeNull();
    expect(screen.queryByText(/No Active Project Loaded/i)).toBeNull();

    // And what IS on screen names the block.
    expect(screen.getByTestId("storage-gate-block")).toBeTruthy();
    expect(screen.getByText(/Durable storage unavailable/i)).toBeTruthy();
  });

  test("the server's own sentence reaches the user, not a stand-in for it", async () => {
    activeOutcome = "storage-gate";
    render(<Page />);
    await settled();

    // Verbatim. A studio that paraphrases the block cannot say which store, and
    // the operator reading it is the one who has to fix the store.
    expect(screen.getByTestId("storage-gate-block").textContent).toContain(GATE_MESSAGE);
  });

  test("it says the work is intact, because that is the question this raises", async () => {
    activeOutcome = "storage-gate";
    render(<Page />);
    await settled();

    const shown = screen.getByTestId("storage-gate-block").textContent || "";
    expect(shown).toMatch(/Nothing has been lost and nothing has been changed/i);
  });

  test("nothing of the film is drawn underneath the block", async () => {
    // Slice 5b: showing nothing beats showing a borrowed stand-in. A banner over
    // a rendered storyboard would not retract what the storyboard asserts.
    activeOutcome = "storage-gate";
    render(<Page />);
    await settled();

    expect(screen.queryByRole("button", { name: /roughcut/i })).toBeNull();
    expect(screen.queryByText(/New Storyboard/i)).toBeNull();
  });
});

describe("the block is stated, never inferred", () => {
  test("a refusal that names the gate but carries no sentence still blocks", async () => {
    // The stated cause is the `storage_gate` key, not the prose beside it. A
    // reply that names the gate and omits the sentence must still block, and
    // must not print the word "undefined" where the explanation goes.
    activeOutcome = "gate-no-message";
    render(<Page />);
    await settled();

    expect(createButton()).toBeNull();
    const shown = screen.getByTestId("storage-gate-block").textContent || "";
    expect(shown).toContain("The durable store could not be reached.");
    expect(shown).not.toContain("undefined");
  });

  test("a 503 that does not name the storage gate is not treated as one", async () => {
    // Reading "the store is down" out of a bare status is the same collapse the
    // backend fix removes -- two different causes rendered as one answer --
    // rebuilt on the client. This must fall through to the ordinary screen.
    activeOutcome = "plain-503";
    render(<Page />);
    await settled();

    expect(screen.queryByTestId("storage-gate-block")).toBeNull();
    expect(createButton()).toBeTruthy();
  });

  test("an unknown project still reads as an unknown project", async () => {
    activeOutcome = "not-found";
    render(<Page />);
    await settled();

    expect(screen.queryByTestId("storage-gate-block")).toBeNull();
    expect(createButton()).toBeTruthy();
  });
});

describe("recovery", () => {
  test("Try again re-reads, and the studio comes back when the store does", async () => {
    // 503 is retryable by definition, which is why the backend answers 503 and
    // not 500. A screen that states a temporary block and offers no way past it
    // strands the user on a reload.
    activeOutcome = "storage-gate";
    render(<Page />);
    await settled();
    expect(screen.getByTestId("storage-gate-block")).toBeTruthy();

    activeOutcome = "ok";
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => expect(screen.queryByTestId("storage-gate-block")).toBeNull());
    expect(screen.getAllByText("Leshy").length).toBeGreaterThan(0);
  });
});
