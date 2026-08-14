/**
 * Switching project, through the real wiring.
 *
 * The component tests mount `MultitrackTimeline` directly and hand it props, so
 * they prove the timeline behaves — and prove nothing at all about whether the
 * studio can reach that behaviour. It could not: `ProjectSidebar` has always
 * called `onSelectProject(p.rel, p.project_id)`, the page wrapper took only
 * `rel`, and `handleSelectProject`'s second parameter was optional, so the id
 * was silently always `undefined`. `projectIdRef` never moved, every later
 * request carried the previous project's `X-Project-Id`, and the middleware
 * honours that header over the active pointer (`main.py:165`) — so the studio
 * answered about the old film forever.
 *
 * No mutation of the timeline could have caught that, because the defect was in
 * the seam between the sidebar and the page. This test lives at that seam: it
 * clicks the sidebar entry a user clicks and watches the headers that go out.
 *
 * The two films deliberately share beat and shot ids, because slot ids are
 * `beat_id::shot_id` and real projects collide on `s001` constantly.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

const FILMS = {
  "film-a": {
    title: "Film A",
    rel: "/w/film-a/storyboard_manifest.json",
    media: "render/s001/s001.1-A.mp4",
    take: "assets/s001/var_A.png",
    summary: "1/1 visuals ready",
  },
  "film-b": {
    title: "Film B",
    rel: "/w/film-b/storyboard_manifest.json",
    media: "",
    take: "assets/s001/var_B.png",
    summary: "0/1 visuals ready · Draft 1 will use 1 placeholder",
  },
} as const;

type FilmId = keyof typeof FILMS;

/** Every request the page made, with the project it named. */
let sent: { url: string; projectId: string | null }[] = [];
/** Which film the server considers active — moved only by /api/project/select. */
let active: FilmId = "film-a";

/** The slot id both films share. This is the collision the stamp must survive. */
const SLOT_ID = "s001::s001.1";

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
        shots: [{
          scene_id: "s001", narration: "", motion_type: "parallax",
          camera: { move: "static", duration: 4 },
          draft_image: f.take, draft_variations: [f.take], chosen_variation: 0,
        }],
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
    return {
      ok: true,
      slots: [{
        id: SLOT_ID, beat_id: "s001", shot_id: "s001.1", index: 0,
        intended_duration: 4, expected_media: "video", media: f.media,
        source_attempt: f.media ? "att-1" : "", trim_in: 0, trim_out: 0,
        placeholder: !f.media, duration: 4,
      }],
      coverage: {
        slots: 1, ready: f.media ? 1 : 0, placeholders: f.media ? 0 : 1,
        runtime: 4, summary: f.summary,
      },
    };
  }
  if (url.startsWith("/api/director/plan/")) {
    return {
      ok: true,
      plan: { beat_id: "s001", coverage: [{ id: "s001.1", draft_variations: [f.take], chosen_variation: 0 }] },
    };
  }
  if (url.startsWith("/api/project/select")) return { ok: true };
  if (url.startsWith("/api/assemble/status")) return { ok: true, jobs: {} };
  if (url.startsWith("/api/metadata")) return { ok: true, metadata: null };
  if (url.startsWith("/api/audio/peaks")) return { ok: true, peaks: {} };
  return { ok: true };
}

beforeEach(() => {
  sent = [];
  active = "film-a";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input).replace("http://localhost:5000", "");
    const headers = new Headers(init?.headers as HeadersInit);
    const named = headers.get("X-Project-Id");
    sent.push({ url, projectId: named });

    // The middleware answers about the project the client NAMED, falling back to
    // the active pointer only when it named none (backend/main.py:165). That is
    // exactly why a stale header is not a cosmetic bug.
    const target = (named || active) as FilmId;
    if (url.startsWith("/api/project/select")) active = "film-b";

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

/** Requests for a URL prefix, after the point the switch was issued. */
const after = (prefix: string) => {
  const at = sent.findIndex((r) => r.url.startsWith("/api/project/select"));
  return sent.slice(at + 1).filter((r) => r.url.startsWith(prefix));
};

const openRoughCut = () =>
  fireEvent.click(screen.getByRole("button", { name: /roughcut/i }));

describe("switching project, through the sidebar the user actually clicks", () => {
  test("the studio retargets, and the new film's cut is what it reads", async () => {
    render(<Page />);
    await waitFor(() => expect(screen.getAllByText("Film A").length).toBeGreaterThan(0));

    openRoughCut();
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe("1/1 visuals ready"));

    // Click film B exactly as a user does.
    fireEvent.click(screen.getAllByText("Film B")[0]);

    // Every request issued after the switch names film B. Before the fix these
    // all carried film-a and the server dutifully answered about film A.
    await waitFor(() => expect(after("/api/project/active").length).toBeGreaterThan(0));
    expect(after("/api/project/active").every((r) => r.projectId === "film-b")).toBe(true);

    await waitFor(() => expect(after("/api/timeline/slots").length).toBeGreaterThan(0));
    expect(after("/api/timeline/slots").every((r) => r.projectId === "film-b")).toBe(true);
  });

  test("film A's cut does not survive the switch, though the slot ids collide", async () => {
    render(<Page />);
    await waitFor(() => expect(screen.getAllByText("Film A").length).toBeGreaterThan(0));
    openRoughCut();

    // Read film A's takes into the strip, so there is something to leak.
    await waitFor(() => expect(screen.getByTestId(`slot-${SLOT_ID}`)).toBeTruthy());
    fireEvent.click(screen.getByTestId(`slot-${SLOT_ID}`));
    await waitFor(() =>
      expect(screen.getByTestId(`slot-${SLOT_ID}`).getAttribute("data-media"))
        .toBe("render/s001/s001.1-A.mp4"));

    fireEvent.click(screen.getAllByText("Film B")[0]);

    // Same slot id, different film. The cut, the coverage sentence and the takes
    // must all be B's — not A's held under an id that happens to match.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent)
        .toBe("0/1 visuals ready · Draft 1 will use 1 placeholder"));

    const clip = screen.getByTestId(`slot-${SLOT_ID}`);
    expect(clip.getAttribute("data-placeholder")).toBe("true");
    expect(clip.getAttribute("data-media")).toBe("");
    expect(screen.queryByTestId(`slot-media-${SLOT_ID}`)).toBeNull();
  });
});
