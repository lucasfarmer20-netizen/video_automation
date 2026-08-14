/**
 * Switching project, through the real wiring.
 *
 * The component tests mount `MultitrackTimeline` directly and hand it props, so
 * they prove the timeline behaves — and prove nothing at all about whether the
 * studio can reach that behaviour. It could not: `ProjectSidebar` has always
 * called `onSelectProject(p.rel, p.project_id)`, the page wrapper took only
 * `rel`, and `handleSelectProject`'s second parameter was optional, so the id
 * was silently always `undefined`. `projectIdRef` never moved, every later
 * request carried the previous film's `X-Project-Id`, and the middleware
 * honours that header over the active pointer (`main.py:165`).
 *
 * These tests live at that seam: they click the sidebar entry a user clicks and
 * watch the headers that go out. The two films deliberately share beat and shot
 * ids, because slot ids are `beat_id::shot_id` and real projects collide on
 * `s001` constantly.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";
import { isStaleReply } from "../lib/directorApi";

const FILMS = {
  "film-a": {
    title: "Film A",
    rel: "/w/film-a/storyboard_manifest.json",
    media: "render/s001/s001.1-A.mp4",
    beatMedia: "render/s002-A.mp4",
    take: "assets/s001/var_A.png",
    summary: "2/2 visuals ready",
  },
  "film-b": {
    title: "Film B",
    rel: "/w/film-b/storyboard_manifest.json",
    media: "",
    beatMedia: "",
    take: "assets/s001/var_B.png",
    summary: "0/2 visuals ready · Draft 1 will use 2 placeholders",
  },
} as const;

type FilmId = keyof typeof FILMS;

/** Every request the page made, with the project it named. */
let sent: { url: string; projectId: string | null }[] = [];
/** Which film the server considers active — moved only by /api/project/select. */
let active: FilmId = "film-a";
/** How /api/project/select behaves. Tests set this before clicking. */
let selectOutcome: "ok" | "refused" | "network-failure" = "ok";

/** The coverage slot both films share — the id collision the stamp must survive. */
const SLOT_ID = "s001::s001.1";
/** A whole-beat slot, whose take swap is a mutating request plus two reads. */
const BEAT_SLOT_ID = "s002::beat";

const beat = (id: string, take: string) => ({
  scene_id: id, narration: "", motion_type: "parallax",
  camera: { move: "static", duration: 4 },
  draft_image: take, draft_variations: [take, take.replace("var_", "var2_")],
  chosen_variation: 0,
});

function payloadFor(url: string, target: FilmId) {
  const f = FILMS[target];
  if (url.startsWith("/api/projects")) {
    return {
      ok: true,
      projects: (Object.keys(FILMS) as FilmId[]).map((id) => ({
        name: FILMS[id].title, rel: FILMS[id].rel, project_id: id,
        rel_display: FILMS[id].title, active: id === active, channel: "bestiary",
      })),
    };
  }
  if (url.startsWith("/api/project/active")) {
    return {
      ok: true,
      project_id: target,
      project: {
        id: target, title: f.title, channel: "bestiary", name: f.title,
        storyboard_approved: true, script_locked: true,
        shots: [beat("s001", f.take), beat("s002", f.take)],
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
    const ready = (f.media ? 1 : 0) + (f.beatMedia ? 1 : 0);
    return {
      ok: true,
      slots: [
        {
          id: SLOT_ID, beat_id: "s001", shot_id: "s001.1", index: 0,
          intended_duration: 4, expected_media: "video", media: f.media,
          source_attempt: f.media ? "att-1" : "", trim_in: 0, trim_out: 0,
          placeholder: !f.media, duration: 4,
        },
        {
          id: BEAT_SLOT_ID, beat_id: "s002", shot_id: "", index: 1,
          intended_duration: 4, expected_media: "video", media: f.beatMedia,
          source_attempt: "", trim_in: 0, trim_out: 0,
          placeholder: !f.beatMedia, duration: 4,
        },
      ],
      coverage: {
        slots: 2, ready, placeholders: 2 - ready, runtime: 8, summary: f.summary,
      },
    };
  }
  if (url.startsWith("/api/director/plan/")) {
    return {
      ok: true,
      plan: {
        beat_id: "s001",
        coverage: [{ id: "s001.1", draft_variations: [f.take], chosen_variation: 0 }],
      },
    };
  }
  if (url.startsWith("/api/assemble/status")) return { ok: true, jobs: {} };
  if (url.startsWith("/api/metadata")) return { ok: true, metadata: null };
  if (url.startsWith("/api/audio/peaks")) return { ok: true, peaks: {} };
  return { ok: true };
}

beforeEach(() => {
  sent = [];
  active = "film-a";
  selectOutcome = "ok";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input).replace("http://localhost:5000", "");
    const headers = new Headers(init?.headers as HeadersInit);
    const named = headers.get("X-Project-Id");
    sent.push({ url, projectId: named });

    // The middleware answers about the project the client NAMED, falling back
    // to the active pointer only when it named none (backend/main.py:165) —
    // which is exactly why a stale header is not a cosmetic problem.
    const target = (named || active) as FilmId;

    if (url.startsWith("/api/project/select")) {
      if (selectOutcome === "network-failure") throw new TypeError("Failed to fetch");
      if (selectOutcome === "refused") {
        // What the studio really gets while a job is running: the endpoint calls
        // refuse_if_jobs_running("switching projects") (backend/main.py:1201).
        return {
          ok: false, status: 409,
          headers: new Headers({ "X-Project-Id": target }),
          json: async () => ({ ok: false, detail: "a render is running — wait for it to finish" }),
          text: async () => JSON.stringify({ detail: "a render is running — wait for it to finish" }),
        } as unknown as Response;
      }
      active = "film-b";
    }

    return {
      ok: true, status: 200,
      headers: new Headers({ "X-Project-Id": target }),
      json: async () => payloadFor(url, target),
      text: async () => JSON.stringify(payloadFor(url, target)),
    } as unknown as Response;
  }));
  vi.stubGlobal("alert", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Requests for a URL prefix, issued after the switch was attempted. */
const after = (prefix: string) => {
  const at = sent.findIndex((r) => r.url.startsWith("/api/project/select"));
  return sent.slice(at + 1).filter((r) => r.url.startsWith(prefix));
};

const openRoughCut = () =>
  fireEvent.click(screen.getByRole("button", { name: /roughcut/i }));

const bootIntoFilmA = async () => {
  render(<Page />);
  await waitFor(() => expect(screen.getAllByText("Film A").length).toBeGreaterThan(0));
  openRoughCut();
  await waitFor(() => expect(screen.getByTestId(`slot-${SLOT_ID}`)).toBeTruthy());
};

/** Swap a take on the whole-beat slot: one mutating POST, then two reads. */
const swapTakeOnBeatSlot = async () => {
  fireEvent.click(screen.getByTestId(`slot-${BEAT_SLOT_ID}`));
  await waitFor(() => expect(screen.getByTestId("slot-take-1")).toBeTruthy());
  fireEvent.click(screen.getByTestId("slot-take-1"));
};

describe("switching project, through the sidebar the user actually clicks", () => {
  test("the studio retargets, and the new film's cut is what it reads", async () => {
    await bootIntoFilmA();
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe("2/2 visuals ready"));

    fireEvent.click(screen.getAllByText("Film B")[0]);

    // Every request issued after the switch names film B. Before the wiring fix
    // these all carried film-a and the server dutifully answered about film A.
    await waitFor(() => expect(after("/api/project/active").length).toBeGreaterThan(0));
    expect(after("/api/project/active").every((r) => r.projectId === "film-b")).toBe(true);

    await waitFor(() => expect(after("/api/timeline/slots").length).toBeGreaterThan(0));
    expect(after("/api/timeline/slots").every((r) => r.projectId === "film-b")).toBe(true);
  });

  test("film A's cut does not survive the switch, though the slot ids collide", async () => {
    await bootIntoFilmA();

    // Read film A's takes into the strip, so there is something to leak.
    fireEvent.click(screen.getByTestId(`slot-${SLOT_ID}`));
    await waitFor(() =>
      expect(screen.getByTestId(`slot-${SLOT_ID}`).getAttribute("data-media"))
        .toBe("render/s001/s001.1-A.mp4"));

    fireEvent.click(screen.getAllByText("Film B")[0]);

    // Same slot id, different film. The cut, the coverage sentence and the takes
    // must all be B's — not A's held under an id that happens to match.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent)
        .toBe("0/2 visuals ready · Draft 1 will use 2 placeholders"));

    const clip = screen.getByTestId(`slot-${SLOT_ID}`);
    expect(clip.getAttribute("data-placeholder")).toBe("true");
    expect(clip.getAttribute("data-media")).toBe("");
    expect(screen.queryByTestId(`slot-media-${SLOT_ID}`)).toBeNull();
  });
});

describe("a switch the server refuses leaves the studio on the film it is showing", () => {
  // /api/project/select refuses whenever a job is running, which is exactly when
  // someone tries to switch and is told no. Identity is committed before the ask,
  // so without a rollback the screen shows film A while everything it says names
  // film B — and the next edit lands in a film nobody is looking at (§11.4).

  test("a refusal rolls the identity back, and says why", async () => {
    await bootIntoFilmA();
    selectOutcome = "refused";

    fireEvent.click(screen.getAllByText("Film B")[0]);
    await waitFor(() => expect(alert).toHaveBeenCalled());

    // The server's reason reaches the user, not a stand-in for it.
    expect((alert as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0])
      .toContain("a render is running");

    // Still film A on screen…
    expect(screen.getByTestId("coverage-summary").textContent).toBe("2/2 visuals ready");

    // …and still film A in everything it says: two reads and one mutation.
    await swapTakeOnBeatSlot();
    await waitFor(() => expect(after("/api/shot/s002").length).toBeGreaterThan(0));
    expect(after("/api/shot/s002").every((r) => r.projectId === "film-a")).toBe(true);

    await waitFor(() => expect(after("/api/timeline/slots").length).toBeGreaterThan(0));
    expect(after("/api/timeline/slots").every((r) => r.projectId === "film-a")).toBe(true);

    await waitFor(() => expect(after("/api/project/active").length).toBeGreaterThan(0));
    expect(after("/api/project/active").every((r) => r.projectId === "film-a")).toBe(true);
  });

  test("a thrown request rolls back the same way", async () => {
    await bootIntoFilmA();
    selectOutcome = "network-failure";

    fireEvent.click(screen.getAllByText("Film B")[0]);
    await waitFor(() => expect(alert).toHaveBeenCalled());

    expect(screen.getByTestId("coverage-summary").textContent).toBe("2/2 visuals ready");

    await swapTakeOnBeatSlot();
    await waitFor(() => expect(after("/api/shot/s002").length).toBeGreaterThan(0));
    expect(after("/api/shot/s002").every((r) => r.projectId === "film-a")).toBe(true);
    expect(after("/api/timeline/slots").every((r) => r.projectId === "film-a")).toBe(true);
    expect(after("/api/project/active").every((r) => r.projectId === "film-a")).toBe(true);
  });

  test("a late reply from the abandoned film is stale after the rollback", async () => {
    // The rollback has to restore the director module's identity too, not only
    // the page ref: `directorApi` keeps its own copy and judges staleness with
    // it. Left on film B, a reply that was genuinely film A's would be thrown
    // away as stale, and film B's would be accepted onto film A's screen.
    await bootIntoFilmA();
    selectOutcome = "refused";

    fireEvent.click(screen.getAllByText("Film B")[0]);
    await waitFor(() => expect(alert).toHaveBeenCalled());

    const fromAbandoned = { headers: new Headers({ "X-Project-Id": "film-b" }) } as Response;
    const fromCurrent = { headers: new Headers({ "X-Project-Id": "film-a" }) } as Response;
    expect(isStaleReply(fromAbandoned)).toBe(true);
    expect(isStaleReply(fromCurrent)).toBe(false);
  });
});
