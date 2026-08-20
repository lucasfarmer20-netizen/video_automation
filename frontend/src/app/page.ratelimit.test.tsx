/**
 * A shed request never becomes "No Active Project Loaded".
 *
 * `page.storage.test.tsx` already establishes the shape of this harm and the
 * reason it is the worst one the studio has: the screen that renders on
 * `!activeProject` has a single call to action, **Initialize Project
 * Workspace**, and offering that over a film which exists and is merely
 * unreachable invites the user to seed a fresh manifest on top of their work.
 *
 * The storage gate closed one door into that screen — the backend's own 503.
 * This closes the other, which is not the backend's at all: Cloud Run's front
 * end sheds load with `429 Rate exceeded.`, `res.json()` threw a SyntaxError,
 * the outer catch logged it to the console, and `fetchActiveProject` returned
 * having installed nothing. The user got the create-a-project screen for the
 * duration of the shed.
 *
 * The first assertion is therefore the absence of that button, exactly as it is
 * in the storage-gate tests: it is the assertion that fails under the
 * regression, and it fails on a claim rather than on a timeout.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

/** How /api/project/active behaves. Tests set this before (or during) a render. */
let activeOutcome: "ok" | "shed" | "gateway-html" | "bad-json" = "ok";

const PROJECT_ID = "leshy";

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

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "X-Project-Id": PROJECT_ID },
  });

/** Exactly what Google's front end sends when it sheds a request. */
const shed = () =>
  new Response("Rate exceeded.", {
    status: 429,
    headers: { "content-type": "text/html; charset=UTF-8" },
  });

beforeEach(() => {
  activeOutcome = "ok";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo) => {
    const url = String(input).replace("http://localhost:5000", "");

    if (url.startsWith("/api/project/active")) {
      if (activeOutcome === "shed") return shed();
      if (activeOutcome === "gateway-html") {
        return new Response("<html><body><h1>502 Server Error</h1></body></html>", {
          status: 502,
          headers: { "content-type": "text/html; charset=UTF-8" },
        });
      }
      if (activeOutcome === "bad-json") {
        return new Response('{"ok": true, "project": {"id": "les', {
          status: 200,
          headers: { "content-type": "application/json", "X-Project-Id": PROJECT_ID },
        });
      }
    }
    return json(200, payloadFor(url));
  }));
  vi.stubGlobal("alert", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A shed read is re-issued twice with a real backoff, so this waits it out. */
const settled = () =>
  waitFor(() => expect(screen.queryByText(/Loading Workspace/i)).toBeNull(), { timeout: 8000 });

const createButton = () =>
  screen.queryByRole("button", { name: /initialize project workspace/i });

describe("a 429 on the active-project read", () => {
  test("never offers to create a project over one it could not reach", async () => {
    activeOutcome = "shed";
    render(<Page />);
    await settled();

    // THE DEFECT, first. Without the block below the page falls through to
    // "No Active Project Loaded" and this button is on screen.
    expect(createButton()).toBeNull();
    expect(screen.queryByText(/No Active Project Loaded/i)).toBeNull();

    expect(screen.getByTestId("load-block")).toBeTruthy();
  }, 20000);

  test("says the request was refused, and that the work is intact", async () => {
    activeOutcome = "shed";
    render(<Page />);
    await settled();

    const shown = screen.getByTestId("load-block").textContent || "";
    expect(shown).toMatch(/rate-limited/i);
    expect(shown).toMatch(/nothing has been lost and nothing has been changed/i);
    // The server's own words, quoted rather than parsed at the user.
    expect(shown).toContain("Rate exceeded.");
    expect(shown).not.toMatch(/unexpected token/i);
  }, 20000);

  test("Try again re-reads, and the studio comes back when the shed passes", async () => {
    activeOutcome = "shed";
    render(<Page />);
    await settled();
    expect(screen.getByTestId("load-block")).toBeTruthy();

    activeOutcome = "ok";
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => expect(screen.queryByTestId("load-block")).toBeNull(), { timeout: 8000 });
    expect(screen.getAllByText("Leshy").length).toBeGreaterThan(0);
  }, 20000);
});

describe("the other replies that are not answers about a project", () => {
  test("a gateway HTML page blocks rather than reading as a missing project", async () => {
    activeOutcome = "gateway-html";
    render(<Page />);
    await settled();

    expect(createButton()).toBeNull();
    expect(screen.getByTestId("load-block")).toBeTruthy();
  }, 20000);

  test("a 200 whose body will not parse blocks too, and quotes no parser", async () => {
    activeOutcome = "bad-json";
    render(<Page />);
    await settled();

    expect(createButton()).toBeNull();
    const shown = screen.getByTestId("load-block").textContent || "";
    expect(shown).not.toMatch(/unexpected token/i);
    expect(shown).toMatch(/could not read the server's reply/i);
  }, 20000);
});
