/**
 * Reading a reply without letting the JSON parser speak for the server.
 *
 * Every fetch helper in this studio used to do some version of
 *
 *     const data = await res.json();
 *
 * and only then look at the status. Two things follow from that order, and both
 * of them reached a human.
 *
 * The first is that a body which is not JSON becomes a *parser* message. Cloud
 * Run's front end sheds load with `429` and a fourteen-byte `Rate exceeded.`
 * body — that string appears nowhere in this repository — so `res.json()` threw
 * `Unexpected token 'R', "Rate exceeded." is not valid JSON`, and that sentence
 * was the only account of the outage anyone got.
 *
 * The second is worse, and it is the reason this module exists rather than a
 * `try` around each call. The studio renders a failed coverage read inside a
 * panel headed **404 / Unplanned Beat Coverage**. So a transport condition — the
 * server was busy for a second and refused to answer — was presented to the
 * human as a specific factual claim about their work: *this beat has no plan*.
 * The plan existed. It was on disk, unchanged, and would have compiled. Telling
 * someone their work is missing when it is not is contract §11.4 with an extra
 * turn: not an unhandled error, but an error rendered as a confident lie.
 *
 * So the discipline here is three rules, and they are ordered:
 *
 *   1. **Status before parsing.** A non-2xx is never parsed for its value.
 *   2. **Content-type before parsing.** A body the server did not label as JSON
 *      is reported verbatim and truncated — what the server said, not what the
 *      parser thought of it.
 *   3. **Transport is not data.** 429 and 5xx say nothing about the resource.
 *      They must never reach the UI wearing a 404's clothes.
 *
 * Retry belongs here too, and it belongs to READS ONLY — see `RetryPolicy`.
 */

/** How much of a non-JSON body is worth showing a human. */
const BODY_EXCERPT = 200;

/**
 * What the studio says when the server shed the request.
 *
 * The second sentence is the whole point of the module. A rate limit is a fact
 * about the request; the human's instinct on seeing any failure in a coverage
 * view is that something happened to their plan, and nothing did.
 */
export const RATE_LIMIT_MESSAGE =
  "The studio is rate-limited — the server refused this request before it " +
  "reached your project. Nothing has been lost and nothing has changed; this " +
  "is a request that was shed, not work that is missing. Try again in a moment.";

/**
 * A failure that is about the connection, not about the thing being read.
 *
 * `transport` is what the UI branches on to keep "the studio could not answer"
 * apart from "the server answered, and the answer is no". `rateLimited` narrows
 * that to the one case that is transient by definition and worth retrying.
 */
export type StudioHttpError = Error & {
  status?: number;
  rateLimited?: boolean;
  transport?: boolean;
  /** What the server actually said, verbatim and truncated. "" when silent. */
  said?: string;
};

/**
 * One reply, read once, with the parser kept away from the human.
 *
 * `data` is `{}` rather than null for an unreadable body so that the existing
 * refusal readers (`data.error`, `data.problems`, `data.approval_drifted`) keep
 * working unchanged against a body that never arrived.
 */
export interface Reply {
  status: number;
  ok: boolean;
  /**
   * The response itself, for the one thing a caller still needs it for: the
   * `X-Project-Id` header the staleness check reads (§11.3). Its body is spent.
   */
  raw: Response;
  /** The parsed body, or `{}` when there was not one to parse. */
  data: JsonBody;
  /** Whether `data` came from an actual JSON body. */
  isJson: boolean;
  /** The server's own words when the body was not JSON. */
  said: string;
  /** 429: shed, transient, safe to re-read. */
  rateLimited: boolean;
  /** 429, any 5xx, or a body that was not JSON: says nothing about the data. */
  transport: boolean;
}

/**
 * A decoded reply body.
 *
 * `any` at the leaves rather than `unknown`: this module reads bodies for two
 * dozen routes with two dozen shapes, and the callers are the ones that know
 * which. Making it `unknown` here would put a cast at every call site instead
 * of a type, which is the same looseness with more code.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type JsonBody = Record<string, any>;

/** Whether a content-type promises something `JSON.parse` can accept. */
function looksJson(contentType: string): boolean {
  return /\bjson\b/i.test(contentType);
}

/**
 * The body as text, or "" if it cannot be had.
 *
 * A body can genuinely be unreadable: once `res.json()` has thrown, the stream
 * is consumed and `res.text()` rejects. "" then means "the server said nothing
 * we can quote", which is a truthful thing to say and better than quoting the
 * parser.
 */
async function bodyText(res: Response): Promise<string> {
  try {
    return (await res.text()).trim();
  } catch {
    return "";
  }
}

/** The server's words, trimmed to something a panel can hold. */
export function excerpt(text: string, max = BODY_EXCERPT): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/**
 * Read a response into a `Reply`, whatever it turns out to be.
 *
 * The content-type is consulted BEFORE parsing, so an HTML gateway page and a
 * plain-text `Rate exceeded.` are never handed to `JSON.parse` at all. When the
 * server sent no content-type the parse is still attempted — some routes are
 * terse — but a failure there is caught and reported as "not JSON" rather than
 * escaping as a SyntaxError.
 */
export async function readReply(res: Response): Promise<Reply> {
  const status = res.status;
  const contentType = res.headers?.get?.("content-type") || "";
  const rateLimited = status === 429;

  let data: JsonBody = {};
  let isJson = false;
  let said = "";

  if (contentType && !looksJson(contentType)) {
    said = excerpt(await bodyText(res));
  } else {
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object") {
        data = parsed as JsonBody;
        isJson = true;
      } else if (parsed !== undefined && parsed !== null) {
        // A bare scalar is valid JSON and still not a reply this studio knows
        // how to read. Quote it rather than pretending it was an object.
        said = excerpt(String(parsed));
      }
    } catch {
      said = excerpt(await bodyText(res));
    }
  }

  return {
    status,
    ok: res.ok,
    raw: res,
    data,
    isJson,
    said,
    rateLimited,
    // A JSON API that answers with HTML was answered by something in FRONT of
    // the app, so the app never saw the request — the same fact a 5xx states.
    transport: rateLimited || status >= 500 || !isJson,
  };
}

/** The server's own sentence, if it sent one, from either field it uses. */
export function serverSaid(reply: Reply): string {
  const { data } = reply;
  const detail =
    data.detail && typeof data.detail === "object"
      ? data.detail.error || ""
      : data.detail;
  const said = data.error || detail;
  return said ? String(said) : "";
}

/**
 * What to tell the human, in the order the truth degrades.
 *
 * A rate limit outranks everything: it is the only case where the studio knows
 * for certain that the server never looked at the resource, so nothing the
 * caller wanted to say about that resource can be true. Below it, a transport
 * failure is quoted rather than characterised. Only once the reply is a real
 * JSON refusal does the server's own sentence get used, because only then is it
 * a statement about the data.
 */
export function failureMessage(reply: Reply, fallback: string): string {
  if (reply.rateLimited) {
    return reply.said ? `${RATE_LIMIT_MESSAGE} (server said: ${reply.said})` : RATE_LIMIT_MESSAGE;
  }
  const said = serverSaid(reply);
  if (said) return said;
  if (!reply.isJson) {
    // The caller's fallback leads, because it is the only part that names what
    // was being done and to which beat. What follows is the correction to the
    // reading a bare failure invites: something answered, it was not the app,
    // and it said nothing about the human's work.
    const quoted = reply.said ? ` The server said: ${reply.said}` : "";
    return (
      `${fallback} The studio could not read the server's reply — this is a ` +
      `failed request, not a statement about your work.${quoted}`
    );
  }
  return fallback;
}

/** Attach the transport facts to an error a caller is already building. */
export function annotate<E extends Error>(err: E, reply: Reply): E & StudioHttpError {
  const out = err as E & StudioHttpError;
  out.status = reply.status;
  out.rateLimited = reply.rateLimited;
  out.transport = reply.transport;
  if (reply.said) out.said = reply.said;
  return out;
}

/** The whole failure, message and facts, for callers with nothing to add. */
export function replyError(reply: Reply, fallback: string): StudioHttpError {
  return annotate(new Error(failureMessage(reply, fallback)), reply);
}

/**
 * How a READ backs off when the server sheds it.
 *
 * Injectable rather than hard-wired because the delays are policy, not
 * behaviour: a caller that must not stall (a poll already on a timer) is
 * entitled to `NO_RETRY`, and a test is entitled to say so without waiting.
 *
 * Deliberately NOT applied to writes. A 429 from Google's front end almost
 * certainly means the request never reached the app — but "almost certainly" is
 * not a basis for re-issuing `POST /api/director/compile`, which buys paid
 * shots. A write that is shed is reported to the human, who re-issues it
 * knowingly. Reads are idempotent, so they retry.
 */
export interface RetryPolicy {
  /** Re-issues after the first shed. 0 disables retry entirely. */
  attempts: number;
  /** Milliseconds to wait before re-issue `n` (1-based). */
  delayMs: (attempt: number) => number;
  sleep: (ms: number) => Promise<void>;
}

export const DEFAULT_RETRY: RetryPolicy = {
  attempts: 2,
  // Linear, and without jitter: this studio is a single browser talking to a
  // single instance, so there is no thundering herd to spread out, and a
  // deterministic delay is one less thing that behaves differently under test.
  delayMs: (attempt) => attempt * 400,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
};

export const NO_RETRY: RetryPolicy = {
  attempts: 0,
  delayMs: () => 0,
  sleep: async () => {},
};

/**
 * Issue a request and read its reply, re-issuing while the server sheds it.
 *
 * Returns the LAST reply when the retries run out — a rate limit that persists
 * is still a rate limit, and the caller says so rather than inventing a
 * different failure for it.
 */
export async function fetchReply(
  url: string,
  init: RequestInit = {},
  retry: RetryPolicy = DEFAULT_RETRY
): Promise<Reply> {
  let reply = await readReply(await fetch(url, init));
  for (let attempt = 1; attempt <= retry.attempts && reply.rateLimited; attempt += 1) {
    await retry.sleep(retry.delayMs(attempt));
    reply = await readReply(await fetch(url, init));
  }
  return reply;
}
