/**
 * `setCoverageStatus` hands its caller everything the server refused it with.
 *
 * `POST /api/director/lock_scene` answers ONE status for every refusal it has —
 * 400 — with the headline `"nothing was locked"` and the difference between them
 * in `problems`. That list is the part that names the beat, the count, and the
 * ids of the findings nobody has decided; the headline names none of the three.
 * Thrown as `new Error(data.error)`, as it was, every sentence the route wrote
 * is discarded at this seam and the component above cannot render what it never
 * received.
 *
 * `DirectorWorkspace`'s own tests mock this module, so nothing there would
 * notice `problems` going missing again. These cover the seam, against the
 * shapes `backend/main.py:2831` and `:2946` really send.
 *
 * Unlocking is deliberately tested as its own thing rather than as lock's
 * mirror. It has no bulk route, so it fans out one request per beat — which
 * means its requests fail INDEPENDENTLY and a scene can come back part
 * unlocked. Locking cannot do that: `lock_scene` validates every beat before it
 * writes any.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import { setCoverageStatus } from "./directorApi";
import type { LockRefusal } from "./directorApi";

/** One canned reply. `body === undefined` means the body was not JSON at all. */
function replyOf(status: number, body: unknown | undefined) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: new Headers(),
    json: async () => {
      if (body === undefined) throw new SyntaxError("Unexpected token < in JSON");
      return body;
    },
  } as unknown as Response;
}

function reply(status: number, body: unknown | undefined) {
  return vi.fn().mockResolvedValue(replyOf(status, body));
}

/** Answer each beat of a fan-out differently, keyed by the beat id in the URL. */
function replyPerBeat(byBeat: Record<string, [number, unknown]>) {
  return vi.fn().mockImplementation(async (url: string) => {
    const hit = Object.keys(byBeat).find((b) => String(url).includes(`/lock/${b}?`));
    if (!hit) throw new Error(`no canned reply for ${url}`);
    return replyOf(byBeat[hit][0], byBeat[hit][1]);
  });
}

/** Throw-and-catch, so the caught value can be inspected rather than matched. */
async function caught(run: () => Promise<unknown>): Promise<LockRefusal> {
  try {
    await run();
  } catch (e) {
    return e as LockRefusal;
  }
  throw new Error("expected setCoverageStatus to throw, and it resolved");
}

/**
 * The sentinel every refusal assertion fails with.
 *
 * It exists so a mutation run can prove the assertion that matters was reached:
 * a mutation that drops `problems` must be killed by the absence of the server's
 * sentence, not by some cheaper check that happens to run first.
 */
const SENTENCE_LOST =
  "the server's own sentence did not survive setCoverageStatus";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("locking a scene: the refusal arrives with what it refused for", () => {
  test("undecided findings — the count and the ids reach the caller", async () => {
    // The refusal the human is actually hitting. `lock_scene`'s own comment:
    // "Contract 5.4: a bulk action must not silently approve unresolved
    // warnings."
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "nothing was locked",
      problems: [
        "s001: 6 critic warning(s) awaiting a decision " +
          "(w_3f2a11, w_91bc04, w_5de720, w_0a8831 and more)",
      ],
    }));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    // THE ASSERTION THIS FILE EXISTS FOR. Everything actionable is in
    // `problems`; the headline names no beat, no count and no finding.
    expect(err.problems, SENTENCE_LOST).toEqual([
      "s001: 6 critic warning(s) awaiting a decision " +
        "(w_3f2a11, w_91bc04, w_5de720, w_0a8831 and more)",
    ]);
    expect(err.message).toBe("nothing was locked");
    expect(err.status).toBe(400);
  });

  test("every beat that failed is reported, not just the first", async () => {
    // The route accumulates one problem per beat and locks none of them. A
    // caller shown only the first would fix it, click again, and be refused for
    // the second — which is the same dead-button experience one step along.
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "nothing was locked",
      problems: [
        "s001: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)",
        "s002: currently compiling",
        "s003: no plan",
      ],
    }));

    const err = await caught(() => setCoverageStatus(["s001", "s002", "s003"], true));

    expect(err.problems, SENTENCE_LOST).toHaveLength(3);
    expect(err.problems?.[1]).toBe("s002: currently compiling");
    expect(err.problems?.[2]).toBe("s003: no plan");
  });

  test("a validation failure arrives as director.validate phrased it", async () => {
    // Not a warning and not a missing plan: the coverage does not fit the beat.
    // The numbers in it are the whole content of the advice.
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "nothing was locked",
      problems: ["s001: coverage totals 31.4s but the beat is 28.2s"],
    }));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    expect(err.problems?.[0], SENTENCE_LOST)
      .toBe("s001: coverage totals 31.4s but the beat is 28.2s");
  });

  test("`beats[] is required` is an HTTPException, so it lives in `detail`", async () => {
    // Raised, not returned, so FastAPI serialises it under `detail` and there
    // is no `error` key. Reading only `error` replaces the sentence that says
    // why with a generic status line.
    vi.stubGlobal("fetch", reply(400, { detail: "beats[] is required" }));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    expect(err.message, SENTENCE_LOST).toBe("beats[] is required");
    expect(err.status).toBe(400);
  });

  test("the catch-all 400 reports whatever actually raised", async () => {
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "no active project",
    }));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    expect(err.message, SENTENCE_LOST).toBe("no active project");
    expect(err.problems).toBeUndefined();
  });

  test("the auth 401 says which header is missing", async () => {
    // `require_studio_key` enforces X-Studio-Key on every non-GET, and the key
    // is read from localStorage — so this is what an unconfigured browser gets
    // for every lock it attempts, forever, with no other symptom.
    vi.stubGlobal("fetch", reply(401, {
      ok: false,
      error: "Missing or invalid X-Studio-Key.",
    }));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    expect(err.message, SENTENCE_LOST).toBe("Missing or invalid X-Studio-Key.");
    expect(err.status).toBe(401);
  });

  test("a non-JSON body still reports the status it failed with", async () => {
    // A proxy 502 is HTML. `res.json()` throwing used to reject with a parser
    // error naming a character offset, which tells the human nothing at all.
    vi.stubGlobal("fetch", reply(502, undefined));

    const err = await caught(() => setCoverageStatus(["s001"], true));

    expect(err.message, SENTENCE_LOST).toContain("502");
    expect(err.message).toContain("s001");
    expect(err.status).toBe(502);
  });

  test("a lock that took returns the reply, and posts the beats it locked", async () => {
    const fetchMock = reply(200, { ok: true, locked: ["s001", "s002"], estimated_cost: 1.85 });
    vi.stubGlobal("fetch", fetchMock);

    const res = await setCoverageStatus(["s001", "s002"], true);

    expect(res.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/director/lock_scene");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      beats: ["s001", "s002"],
    });
  });
});

describe("locking one beat: the route that names its own count", () => {
  test("the undecided findings come back with the finding list", async () => {
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error:
        "s001 has 2 critic warning(s) awaiting a decision; resolve or accept " +
        "each one before locking.",
      warnings: [
        { id: "w_3f2a11", kind: "identity_risk", detail: "s001.03 shows the face at 3.1s" },
        { id: "w_91bc04", kind: "repeated_framing", detail: "s001.05 repeats s001.02" },
      ],
    }));

    const err = await caught(() => setCoverageStatus("s001", true));

    expect(err.message, SENTENCE_LOST).toBe(
      "s001 has 2 critic warning(s) awaiting a decision; resolve or accept " +
        "each one before locking."
    );
    expect(err.warnings).toHaveLength(2);
    expect(err.status).toBe(400);
  });

  test("no plan for the beat is a 404 with its own sentence", async () => {
    vi.stubGlobal("fetch", reply(404, { ok: false, error: "no plan for s001" }));

    const err = await caught(() => setCoverageStatus("s001", true));

    expect(err.message, SENTENCE_LOST).toBe("no plan for s001");
    expect(err.status).toBe(404);
  });

  test("a compiling beat is a 409, and it is not the user's mistake", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error: "s001 is compiling; cannot change its status",
    }));

    const err = await caught(() => setCoverageStatus("s001", true));

    expect(err.message, SENTENCE_LOST).toBe("s001 is compiling; cannot change its status");
    expect(err.status).toBe(409);
  });
});

describe("unlocking a scene: not the mirror of locking", () => {
  test("the refusal names the beat that refused, in the route's words", async () => {
    vi.stubGlobal("fetch", replyPerBeat({
      s001: [409, { ok: false, error: "s001 is compiling; cannot change its status" }],
    }));

    const err = await caught(() => setCoverageStatus(["s001"], false));

    expect(err.message, SENTENCE_LOST).toBe("s001 is compiling; cannot change its status");
    expect(err.problems, SENTENCE_LOST)
      .toEqual(["s001: s001 is compiling; cannot change its status"]);
    expect(err.changed).toEqual([]);
  });

  test("a beat with no plan is a 404 the caller can still read", async () => {
    vi.stubGlobal("fetch", replyPerBeat({
      s001: [404, { ok: false, error: "no plan for s001" }],
    }));

    const err = await caught(() => setCoverageStatus(["s001"], false));

    expect(err.message, SENTENCE_LOST).toBe("no plan for s001");
    expect(err.status).toBe(404);
  });

  test("the beats that DID unlock are reported with the refusal", async () => {
    // The asymmetry that matters. `lock_scene` validates everything before it
    // writes anything, so a refused lock changed nothing. Unlocking has no bulk
    // route: these requests are independent, they all go, and a scene comes
    // back part unlocked. A caller told only "s002 is compiling" would leave a
    // screen claiming the whole scene is still locked when half of it is not.
    vi.stubGlobal("fetch", replyPerBeat({
      s001: [200, { ok: true, beat_id: "s001", status: "draft" }],
      s002: [409, { ok: false, error: "s002 is compiling; cannot change its status" }],
      s003: [200, { ok: true, beat_id: "s003", status: "draft" }],
    }));

    const err = await caught(() => setCoverageStatus(["s001", "s002", "s003"], false));

    expect(err.changed, SENTENCE_LOST).toEqual(["s001", "s003"]);
    expect(err.problems).toEqual(["s002: s002 is compiling; cannot change its status"]);
  });

  test("several refusals are all reported, and the headline says how many", async () => {
    vi.stubGlobal("fetch", replyPerBeat({
      s001: [404, { ok: false, error: "no plan for s001" }],
      s002: [409, { ok: false, error: "s002 is compiling; cannot change its status" }],
    }));

    const err = await caught(() => setCoverageStatus(["s001", "s002"], false));

    expect(err.problems, SENTENCE_LOST).toEqual([
      "s001: no plan for s001",
      "s002: s002 is compiling; cannot change its status",
    ]);
    // No single sentence to promote, so the headline counts rather than picking.
    expect(err.message).toBe("2 of 2 beats refused to unlock.");
    expect(err.changed).toEqual([]);
  });

  test("a beat answered with no readable body is still a refusal that says so", async () => {
    vi.stubGlobal("fetch", replyPerBeat({ s001: [502, undefined] }));

    const err = await caught(() => setCoverageStatus(["s001"], false));

    // `r.ok` is what catches this: the body has no `ok` field to be falsy.
    // `toContain` rather than `toBe`, because the sentence now carries a second
    // clause saying the reply was unreadable — a 502 HTML page is something in
    // FRONT of the app answering, so it states nothing about this beat. The
    // part asserted here is the part the server's own refusal would occupy.
    expect(err.problems?.[0], SENTENCE_LOST).toContain(
      "s001: unlocking failed with status 502"
    );
  });

  test("unlocking sends locked=false per beat, never the bulk lock route", async () => {
    // The array branch used to run for every call and ignore `locked`, so
    // "UNLOCK TO EDIT" locked harder while the screen showed "draft".
    const fetchMock = replyPerBeat({
      s001: [200, { ok: true, status: "draft" }],
      s002: [200, { ok: true, status: "draft" }],
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await setCoverageStatus(["s001", "s002"], false);

    expect(res.status).toBe("draft");
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.every((u) => u.includes("locked=false"))).toBe(true);
    expect(urls.some((u) => u.includes("lock_scene"))).toBe(false);
  });
});
