/**
 * Creating and deleting a project, through the real wiring (issues #7, #8).
 *
 * Both defects are the same class as the one slice 5b spent six rounds on in
 * `handleSelectProject`: a handler moves project identity on one side and not
 * the other, so the client ends up naming a project the server is not on — or
 * the server moves and the client does not follow.
 *
 * They live in the sidebar wiring, which is why these are page-level tests.
 * Mounting `ProjectSidebar` and handing it props would prove the buttons call
 * their callbacks and prove nothing about `projectIdRef`, which is where both
 * bugs are. So the fake server below models the two things that actually make
 * them bite:
 *
 *   * `bind_project_context` honours `X-Project-Id` over the active pointer
 *     (backend/main.py:165) — a client naming the wrong project is answered
 *     about the wrong project, deliberately;
 *   * `_context_for` resolves foreign ids by scanning `_scan_projects()`, which
 *     skips `_trash` (backend/main.py:197), so a deleted project's id stops
 *     resolving and every request carrying it 404s.
 *
 * The 404 is returned the way the middleware returns it: before the header is
 * attached, so the reply does not name a project at all.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";
import { isStaleReply } from "../lib/directorApi";

type FilmId = "film-a" | "film-b" | "film-c";

interface Film {
  title: string;
  rel: string;
  /** Coverage sentence — the cheapest proof of WHICH film is on screen. */
  summary: string;
  media: string;
  beatMedia: string;
  take: string;
  /** Moved to _trash. Still on disk, and deliberately unresolvable. */
  trashed: boolean;
}

const FRESH: Record<FilmId, Film> = {
  "film-a": {
    title: "Film A", rel: "/w/film-a/storyboard_manifest.json",
    summary: "2/2 visuals ready",
    media: "render/s001/s001.1-A.mp4", beatMedia: "render/s002-A.mp4",
    take: "assets/s001/var_A.png", trashed: false,
  },
  "film-b": {
    title: "Film B", rel: "/w/film-b/storyboard_manifest.json",
    summary: "0/2 visuals ready · Draft 1 will use 2 placeholders",
    media: "", beatMedia: "", take: "assets/s001/var_B.png", trashed: false,
  },
  // Only exists once /api/project/new has been called.
  "film-c": {
    title: "Film C", rel: "/w/film-c/storyboard_manifest.json",
    summary: "1/2 visuals ready · Draft 1 will use 1 placeholder",
    media: "render/s001/s001.1-C.mp4", beatMedia: "",
    take: "assets/s001/var_C.png", trashed: true,   // "does not exist yet"
  },
};

let films: Record<FilmId, Film>;
/** Which film the server's active pointer names. */
let active: FilmId;
/** Every request the page made, with the project it named. */
let sent: { url: string; projectId: string | null }[] = [];
/** Requests the middleware refused because the id no longer resolves. */
let refused: { url: string; projectId: string | null }[] = [];
/** How /api/project/active behaves — the authoritative load. */
let activeOutcome: "ok" | "transport" = "ok";
/** Set to make the NEXT /api/project/active hang until it is released. */
let heldLoad: Promise<void> | null = null;
let releaseLoad: (() => void) | null = null;
const hangTheNextLoad = () => {
  heldLoad = new Promise<void>((resolve) => { releaseLoad = resolve; });
};
/** Whether /api/project/new returns the additive `project_id` field. */
let newReturnsId = true;
/** What window.prompt answers next. */
let promptAnswer = "";

const SLOT_ID = "s001::s001.1";
const BEAT_SLOT_ID = "s002::beat";

const beat = (id: string, take: string) => ({
  scene_id: id, narration: "", motion_type: "parallax",
  camera: { move: "static", duration: 4 },
  draft_image: take, draft_variations: [take, take.replace("var_", "var2_")],
  chosen_variation: 0,
});

/** Ids the sidebar is currently listing, in the order it lists them. */
const listed = (): FilmId[] =>
  (Object.keys(films) as FilmId[]).filter((id) => !films[id].trashed);

function payloadFor(url: string, target: FilmId) {
  const f = films[target];
  if (url.startsWith("/api/projects")) {
    return {
      ok: true,
      projects: listed().map((id) => ({
        name: films[id].title, rel: films[id].rel, project_id: id,
        rel_display: films[id].title, active: id === active, channel: "bestiary",
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

const reply = (body: any, target: FilmId) => ({
  ok: true, status: 200,
  headers: new Headers({ "X-Project-Id": target }),
  json: async () => body,
  text: async () => JSON.stringify(body),
}) as unknown as Response;

beforeEach(() => {
  films = JSON.parse(JSON.stringify(FRESH));
  active = "film-a";
  sent = [];
  refused = [];
  activeOutcome = "ok";
  newReturnsId = true;
  promptAnswer = "";
  heldLoad = null;
  releaseLoad = null;

  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input).replace("http://localhost:5000", "");
    const headers = new Headers(init?.headers as HeadersInit);
    const named = headers.get("X-Project-Id");
    sent.push({ url, projectId: named });

    // bind_project_context, faithfully: the named project wins over the pointer,
    // and a name that does not resolve is refused rather than quietly served the
    // active one. The refusal is built BEFORE the X-Project-Id header is
    // attached, exactly as the middleware does it (backend/main.py:169-172), so
    // the reply names no project and the client's staleness check cannot fire.
    const resolvable = (id: string): id is FilmId =>
      id in films && !films[id as FilmId].trashed;
    if (named && !resolvable(named)) {
      refused.push({ url, projectId: named });
      return {
        ok: false, status: 404,
        headers: new Headers(),
        json: async () => ({ ok: false, error: `Unknown project id: ${named}`, project_id: named }),
        text: async () => JSON.stringify({ ok: false, error: `Unknown project id: ${named}` }),
      } as unknown as Response;
    }
    const target = (named || active) as FilmId;

    if (url.startsWith("/api/project/delete")) {
      const rel = JSON.parse(String(init?.body || "{}")).rel;
      const hit = (Object.keys(films) as FilmId[]).find((id) => films[id].rel === rel)!;
      const was_active = hit === active;
      films[hit].trashed = true;
      // "Never leave the studio pointed at a directory that no longer exists"
      // (backend/main.py:1294-1303): the server repoints to whatever remains,
      // and tells the client only THAT it did.
      if (was_active) active = listed()[0];
      return reply({
        ok: true, deleted: films[hit].title, purged: false,
        moved_to: `_trash/${hit}`, bytes: 1048576, was_active,
      }, target);
    }

    if (url.startsWith("/api/project/new")) {
      films["film-c"].trashed = false;
      films["film-c"].title = JSON.parse(String(init?.body || "{}")).name || "Film C";
      active = "film-c";
      return reply({
        ok: true,
        rel: films["film-c"].rel,
        ...(newReturnsId ? { project_id: "film-c" } : {}),
      }, target);
    }

    if (url.startsWith("/api/project/active")) {
      if (activeOutcome === "transport") throw new TypeError("Failed to fetch");
      // Hold this one load open, so a test can look at the studio while it is
      // between projects rather than only at where it ends up.
      if (heldLoad) {
        const held = heldLoad;
        heldLoad = null;
        await held;
      }
    }

    return reply(payloadFor(url, target), target);
  }));
  vi.stubGlobal("alert", vi.fn());
  vi.stubGlobal("prompt", vi.fn(() => promptAnswer));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Requests to `prefix` issued after `marker` went out. */
const after = (marker: string, prefix: string) => {
  const at = sent.findIndex((r) => r.url.startsWith(marker));
  return sent.slice(at + 1).filter((r) => r.url.startsWith(prefix));
};

/**
 * Assert every request to `prefix` after `marker` named `projectId` — and that
 * there was at least one.
 *
 * The length check is the whole point, and it is 5b's lesson: `[].every(...)` is
 * vacuously true, so asserting `.every()` alone keeps passing if the flow stops
 * issuing the request at all. The test would assert nothing and still be green.
 */
const expectAllNamed = async (marker: string, prefix: string, projectId: string | null) => {
  await waitFor(() => expect(after(marker, prefix).length).toBeGreaterThan(0));
  expect(after(marker, prefix).map((r) => r.projectId).every((p) => p === projectId)).toBe(true);
};

const openRoughCut = () =>
  fireEvent.click(screen.getByRole("button", { name: /roughcut/i }));

const bootIntoFilmA = async () => {
  render(<Page />);
  await waitFor(() => expect(screen.getAllByText("Film A").length).toBeGreaterThan(0));
  openRoughCut();
  await waitFor(() => expect(screen.getByTestId(`slot-${SLOT_ID}`)).toBeTruthy());
  await waitFor(() =>
    expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-a"].summary));
};

/** Click the trash button on the sidebar card for `id`, and confirm. */
const deleteFilm = (id: FilmId) => {
  promptAnswer = films[id].title;
  const index = listed().indexOf(id);
  fireEvent.click(screen.getAllByTitle(/Delete this storyboard/)[index]);
};

const createFilm = (name: string) => {
  promptAnswer = name;
  fireEvent.click(screen.getByRole("button", { name: /new storyboard/i }));
};

/** Swap a take on the whole-beat slot: one mutating POST, then two reads. */
const swapTakeOnBeatSlot = async () => {
  fireEvent.click(screen.getByTestId(`slot-${BEAT_SLOT_ID}`));
  await waitFor(() => expect(screen.getByTestId("slot-take-1")).toBeTruthy());
  fireEvent.click(screen.getByTestId("slot-take-1"));
};

const replyFrom = (id: string) =>
  ({ headers: new Headers({ "X-Project-Id": id }) }) as Response;

describe("issue #7 — deleting the ACTIVE project", () => {
  test("the studio lands on the project the server repointed to, with no 404 storm", async () => {
    // The defect: handleDeleteProject never touched projectIdRef, so every later
    // request named a project now in _trash. The first 404 was the load meant to
    // recover, and fetchActiveProject — seeing data.ok false — updated nothing,
    // so nothing reached the screen. Only a reload got out of it.
    await bootIntoFilmA();

    deleteFilm("film-a");

    // The server's pick is on screen, which is only possible if the client
    // stopped naming the dead film.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-b"].summary));

    // Nothing after the delete was refused. Before the fix, everything was.
    expect(refused).toEqual([]);

    // And no request after it names the deleted film — including the ones the
    // 3s job poller keeps issuing, which is what made this a storm and not a
    // single failure.
    expect(after("/api/project/delete", "/api").filter((r) => r.projectId === "film-a"))
      .toEqual([]);

    // The identity moved, not just the pixels: a real edit made now is written
    // to the film the server is on.
    await swapTakeOnBeatSlot();
    await expectAllNamed("/api/project/delete", "/api/shot/s002", "film-b");
    await expectAllNamed("/api/project/delete", "/api/timeline/slots", "film-b");
  });

  test("deleting some OTHER project does not move the studio at all", async () => {
    // The `was_active` gate. Clearing identity unconditionally would hand the
    // next request to the server's active pointer, which is free to name a film
    // the user is not looking at — the same defect, arrived at from the fix.
    await bootIntoFilmA();

    deleteFilm("film-b");
    await waitFor(() => expect(alert).toHaveBeenCalled());

    // Still film A on screen, and still film A in everything it says.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-a"].summary));
    await expectAllNamed("/api/project/delete", "/api/project/active", "film-a");

    await swapTakeOnBeatSlot();
    await expectAllNamed("/api/project/delete", "/api/shot/s002", "film-a");
    expect(refused).toEqual([]);
  });

  test("the workspace is gone while the studio is between projects", async () => {
    // The window this closes: identity has been dropped, so requests carry no
    // id and the server's active pointer answers them — while the screen still
    // shows the deleted film. A control clicked here would write to whichever
    // film the server picked. Much smaller than the wedge, and the same class,
    // so it is closed the same way: there is nothing to click, because
    // `whileLoading` takes the workspace away until the replacement lands.
    await bootIntoFilmA();
    hangTheNextLoad();

    deleteFilm("film-a");

    await waitFor(() => expect(screen.getByText(/Loading Workspace/i)).toBeTruthy());
    expect(screen.queryByTestId(`slot-${BEAT_SLOT_ID}`)).toBeNull();
    expect(screen.queryByTestId("coverage-summary")).toBeNull();

    releaseLoad!();

    // And it comes back down on the film the server repointed to.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-b"].summary));
    expect(screen.queryByText(/Loading Workspace/i)).toBeNull();
  });

  test("a delete whose reload fails says nothing is loaded, not the deleted film", async () => {
    // There is nothing to roll back to here — the film on screen is in _trash.
    // Leaving it up would invite an edit on a project that no longer exists, so
    // the studio has to state the block rather than substitute a stand-in.
    await bootIntoFilmA();
    activeOutcome = "transport";

    deleteFilm("film-a");
    await waitFor(() => expect(alert).toHaveBeenCalled());

    await waitFor(() => expect(screen.getByText(/No Active Project Loaded/i)).toBeTruthy());
    expect((alert as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0])
      .toContain("could not load the project the server switched to");

    // The deleted film is not on screen under any reading.
    expect(screen.queryByTestId("coverage-summary")).toBeNull();

    // And the director module names no project either, so a reply from the film
    // the server actually switched to is not thrown away as someone else's.
    // Left on the deleted id, it would be.
    expect(isStaleReply(replyFrom("film-b"))).toBe(false);
  });
});

describe("issue #8 — creating a project", () => {
  test("the studio lands in the new film, and the load itself names it", async () => {
    // The defect: /api/project/new repoints the pointer, but the follow-up load
    // still carried the OLD X-Project-Id — and the middleware honours the header
    // over the pointer by design. The server answered about the previous film,
    // data.project_id came back as the previous film, and the ref never moved.
    await bootIntoFilmA();

    createFilm("Film C");

    // Issue #8's definition of done: the outgoing headers name the new project.
    // Not "the pointer eventually agrees" — the request itself.
    await expectAllNamed("/api/project/new", "/api/project/active", "film-c");

    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-c"].summary));
    expect(refused).toEqual([]);

    await swapTakeOnBeatSlot();
    await expectAllNamed("/api/project/new", "/api/shot/s002", "film-c");
  });

  test("it lands there too when the server omits project_id", async () => {
    // The documented fallback, kept so an older server degrades to a header-less
    // read — answered by the active pointer the create just repointed — rather
    // than back to the defect. Weaker than naming the id (there is a window in
    // which the request names nothing), which is why it is the fallback.
    await bootIntoFilmA();
    newReturnsId = false;

    createFilm("Film C");

    // The first load names no project, and the pointer answers it…
    await waitFor(() =>
      expect(after("/api/project/new", "/api/project/active").length).toBeGreaterThan(0));
    expect(after("/api/project/new", "/api/project/active")[0].projectId).toBeNull();

    // …after which the id the server reported is adopted and named explicitly.
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-c"].summary));
    await swapTakeOnBeatSlot();
    await expectAllNamed("/api/project/new", "/api/shot/s002", "film-c");
  });

  test("a create whose load fails leaves the studio on the previous film, named", async () => {
    // Identity is committed before the load confirms it, so the path where the
    // load fails has to put it back — handleSelectProject's lesson, applied here
    // because the same premise holds: the previous film still exists. Without
    // the rollback the studio shows film A while every request names film C.
    await bootIntoFilmA();
    activeOutcome = "transport";

    createFilm("Film C");
    await waitFor(() => expect(alert).toHaveBeenCalled());
    expect((alert as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0])
      .toContain("still showing the previous film");
    activeOutcome = "ok";

    // Film A is on screen…
    await waitFor(() =>
      expect(screen.getByTestId("coverage-summary").textContent).toBe(films["film-a"].summary));

    // …so an edit made here must reach film A, not the film the server's pointer
    // moved to underneath it.
    await swapTakeOnBeatSlot();
    await expectAllNamed("/api/project/new", "/api/shot/s002", "film-a");
    await expectAllNamed("/api/project/new", "/api/timeline/slots", "film-a");
    expect(isStaleReply(replyFrom("film-c"))).toBe(true);
  });
});
