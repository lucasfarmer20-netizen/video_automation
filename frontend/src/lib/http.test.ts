/**
 * A reply is read for what it is, and a transport condition never becomes a
 * claim about the human's work.
 *
 * The defect this pins: `GET /api/project/active` was answered `429` with a
 * fourteen-byte `Rate exceeded.` body by Cloud Run's front end. `res.json()`
 * threw `Unexpected token 'R', "Rate exceeded." is not valid JSON`, and the
 * studio rendered that string inside a panel headed **404 / Unplanned Beat
 * Coverage** — so a one-second load shed was presented to a director as the
 * finding that their beat had no plan. It had one.
 *
 * Every test below therefore asserts the FALSE CLAIM's absence first. The
 * parser's complaint leaking through is a bug; being told your work is missing
 * when it is not is the harm, and it is what the first assertion in each test
 * is about.
 */
import { describe, expect, test, vi } from "vitest";
import {
  DEFAULT_RETRY,
  NO_RETRY,
  RetryPolicy,
  excerpt,
  failureMessage,
  fetchReply,
  readReply,
} from "./http";

/** What the studio must never say because a request failed. */
const CLAIMS_ABOUT_THE_WORK = [
  /no plan/i,
  /not found/i,
  /unplanned/i,
  /does not exist/i,
  /\b404\b/,
];

/** What the JSON parser says, which is never a fact about a beat. */
const PARSER_COMPLAINTS = [/unexpected token/i, /is not valid json/i, /syntaxerror/i];

/**
 * The defect-proving assertion, and it goes first everywhere it appears.
 *
 * Ordered before the positive checks deliberately: if this file is ever read to
 * find out what actually matters here, the answer is on the first line of each
 * test — the studio does not tell someone their work is gone because a request
 * was refused.
 */
function expectsNothingAboutTheWork(message: string) {
  CLAIMS_ABOUT_THE_WORK.forEach((claim) =>
    expect(message, `a failed request must not claim ${claim}`).not.toMatch(claim)
  );
  PARSER_COMPLAINTS.forEach((complaint) =>
    expect(message, `the parser must not speak for the server`).not.toMatch(complaint)
  );
}

/** Exactly what Google's front end sends when it sheds a request. */
const shed = () =>
  new Response("Rate exceeded.", {
    status: 429,
    headers: { "content-type": "text/html; charset=UTF-8" },
  });

/** A gateway error page, which is HTML and says nothing about any project. */
const gatewayHtml = (status: number) =>
  new Response(
    `<html><head><title>${status}</title></head><body><h1>${status} Server Error</h1></body></html>`,
    { status, headers: { "content-type": "text/html; charset=UTF-8" } }
  );

/** A 200 whose body was truncated mid-flight. */
const truncatedJson = () =>
  new Response('{"ok": true, "beats": [{"beat_id": "s0', {
    status: 200,
    headers: { "content-type": "application/json" },
  });

/** The app's own 404, which IS a statement about the resource. */
const realNotFound = () =>
  new Response(JSON.stringify({ detail: "No coverage plan found for s006" }), {
    status: 404,
    headers: { "content-type": "application/json" },
  });

describe("429, plain text: the studio was refused, and says so", () => {
  test("the message makes no claim about the beat, and quotes the server", async () => {
    const reply = await readReply(shed());
    const message = failureMessage(reply, "Failed to fetch scene coverage for beats=s006.");

    expectsNothingAboutTheWork(message);

    expect(message).toMatch(/rate-limited/i);
    expect(message).toMatch(/nothing has been lost/i);
    // The server's own words survive, truncated — a quote, not a diagnosis.
    expect(message).toContain("Rate exceeded.");
  });

  test("the reply carries the facts a UI can branch on", async () => {
    const reply = await readReply(shed());

    expect(reply.rateLimited, "a 429 is the one failure that is transient by definition").toBe(true);
    expect(reply.transport).toBe(true);
    expect(reply.isJson).toBe(false);
    expect(reply.status).toBe(429);
    expect(reply.said).toBe("Rate exceeded.");
  });
});

describe("5xx HTML: something in front of the app answered", () => {
  test("the gateway's page is quoted, never parsed at the human", async () => {
    const reply = await readReply(gatewayHtml(502));
    const message = failureMessage(reply, "Locking s001 failed with status 502.");

    expectsNothingAboutTheWork(message);

    // The caller's own sentence leads: it is the only part that names what was
    // being done and to which beat.
    expect(message).toContain("Locking s001 failed with status 502.");
    expect(message).toMatch(/not a statement about your work/i);
    expect(reply.transport).toBe(true);
    expect(reply.rateLimited, "a 502 is not a rate limit and must not be retried as one").toBe(false);
  });
});

describe("200 with malformed JSON: a reply that cannot be read is not an answer", () => {
  test("it reports an unreadable reply, not a missing plan", async () => {
    const reply = await readReply(truncatedJson());
    const message = failureMessage(reply, "Failed to fetch scene coverage for beats=s006.");

    expectsNothingAboutTheWork(message);

    expect(reply.isJson, "a body that will not parse has not been read").toBe(false);
    expect(reply.transport).toBe(true);
    expect(reply.ok, "the status was 200, and the status is not the problem").toBe(true);
    expect(message).toMatch(/could not read the server's reply/i);
  });
});

describe("a real 404 still reads as not found", () => {
  test("the server's own sentence is what reaches the human", async () => {
    const reply = await readReply(realNotFound());
    const message = failureMessage(reply, "Failed to fetch scene coverage for beats=s006.");

    // The one place the opposite direction is the defect: over-generalising the
    // fix would swallow a genuine 404 into "the studio could not answer", which
    // hides a real missing plan behind a transport excuse.
    expect(message).toBe("No coverage plan found for s006");
    expect(reply.transport, "the app answered, in JSON, about this resource").toBe(false);
    expect(reply.rateLimited).toBe(false);
    expect(reply.isJson).toBe(true);
  });
});

describe("backing off, because a 429 is transient by definition", () => {
  /** A policy with no waiting in it, so a test measures behaviour not time. */
  const instant: RetryPolicy = { attempts: 2, delayMs: () => 0, sleep: async () => {} };

  test("a read that is shed and then answered resolves, and nothing is claimed", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(shed())
      .mockResolvedValueOnce(shed())
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true, beats: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    const reply = await fetchReply("/api/director/scene?beats=s006", {}, instant);

    expect(reply.data.ok, "the read succeeded on the third try and must be used").toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    vi.unstubAllGlobals();
  });

  test("a shed that persists is still reported as a shed, not as something else", async () => {
    const fetchMock = vi.fn().mockResolvedValue(shed());
    vi.stubGlobal("fetch", fetchMock);

    const reply = await fetchReply("/api/director/scene?beats=s006", {}, instant);
    expectsNothingAboutTheWork(failureMessage(reply, "Failed to fetch scene coverage."));

    expect(reply.rateLimited).toBe(true);
    expect(fetchMock, "one attempt plus two re-issues").toHaveBeenCalledTimes(3);
    vi.unstubAllGlobals();
  });

  test("a 502 is not re-issued: only the shed status is transient", async () => {
    const fetchMock = vi.fn().mockResolvedValue(gatewayHtml(503));
    vi.stubGlobal("fetch", fetchMock);

    await fetchReply("/api/director/scene?beats=s006", {}, instant);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  test("NO_RETRY issues exactly one request, which is what a write needs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(shed());
    vi.stubGlobal("fetch", fetchMock);

    await fetchReply("/api/director/compile/s006", { method: "POST" }, NO_RETRY);

    expect(
      fetchMock,
      "a compile buys paid shots; a shed one is reported, never re-issued"
    ).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  test("the default policy re-issues a read twice and waits between", async () => {
    // Pinned because the default is what every read actually uses; a policy that
    // silently became `attempts: 0` would leave the retry documented and absent.
    expect(DEFAULT_RETRY.attempts).toBe(2);
    expect(DEFAULT_RETRY.delayMs(1)).toBeGreaterThan(0);
    expect(DEFAULT_RETRY.delayMs(2)).toBeGreaterThan(DEFAULT_RETRY.delayMs(1));
  });
});

describe("quoting the server without letting it take over the screen", () => {
  test("a long body is truncated and marked as truncated", () => {
    const shown = excerpt("x".repeat(500));

    expect(shown.length).toBeLessThan(500);
    expect(shown.endsWith("…")).toBe(true);
  });

  test("a short body is quoted whole, with its whitespace collapsed", () => {
    expect(excerpt("  Rate   exceeded.\n")).toBe("Rate exceeded.");
  });
});
