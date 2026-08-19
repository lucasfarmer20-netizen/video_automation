/**
 * The studio names the price before it spends, and sends the price it named.
 *
 * "Generate Video" on a beat card posts to /api/shot/{id}/generate_video, which
 * buys a Tier-C clip from fal. The confirm read:
 *
 *     🎬 PAID: Video generation calls fal.ai. Continue?
 *
 * — for a call that ranges from $0.28 to $6.00 depending on the model, the
 * beat's length and whether the audio toggle is on. The request carried no
 * price either, so nothing between this button and the provider knew what it
 * cost. The server now refuses an unpriced request; this file is the other half
 * of that, and the half a human actually sees.
 *
 * ASSERTION ORDER, as everywhere this repo touches money: the thing that spends
 * goes first. "no POST was made" beats "the dialog said the right thing" —
 * a confirm with the wrong wording is a bad dialog, a POST with no accepted
 * price is a charge.
 *
 * The price is deliberately NOT computed in the browser. It is fetched from the
 * endpoint that also bills, because a second pricing implementation next to the
 * one that charges is how the quote and the ledger came to disagree.
 */
import React from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

const BEAT = "s001";

/** 8s of veo_3_1 with audio: 8 x $0.40. The silent rate is half of it. */
const QUOTE = {
  video_model: "veo_3_1",
  generate_seconds: 8,
  generate_audio: true,
  video_cost: 3.2,
  stills: 0,
  still_cost: 0,
  estimated_cost: 3.2,
};

let posts: { url: string; body: any }[] = [];
let confirms: string[] = [];
let quoteReachable = true;

const reply = (status: number, body: unknown) => ({
  ok: status < 400,
  status,
  headers: new Headers({ "X-Project-Id": "leshy" }),
  json: async () => body,
  text: async () => JSON.stringify(body),
}) as unknown as Response;

const beat = {
  scene_id: BEAT,
  narration: "n",
  prompt: "p",
  motion_type: "ai_video",
  video_model: "veo_3_1",
  camera: { move: "static", duration: 8 },
  draft_image: "assets/s001/still.png",
  draft_variations: ["assets/s001/still.png"],
  chosen_variation: 0,
};

beforeEach(() => {
  posts = [];
  confirms = [];
  quoteReachable = true;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input).replace("http://localhost:5000", "");
    if ((init?.method || "GET").toUpperCase() === "POST") {
      posts.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return reply(200, { ok: true, video_path: `/render/${BEAT}.mp4` });
    }
    if (url.startsWith(`/api/shot/${BEAT}/video_quote`)) {
      return quoteReachable
        ? reply(200, { ok: true, scene_id: BEAT, quote: QUOTE })
        : reply(503, { ok: false, error: "storage unreachable" });
    }
    if (url.startsWith("/api/project/active")) {
      return reply(200, {
        ok: true,
        project_id: "leshy",
        project: {
          id: "leshy", title: "T", channel: "bestiary", name: "T",
          storyboard_approved: true, script_locked: true, shots: [beat],
        },
      });
    }
    if (url.startsWith("/api/projects")) {
      return reply(200, { ok: true, projects: [] });
    }
    if (url.startsWith("/api/assemble/status")) return reply(200, { ok: true, jobs: {} });
    if (url.startsWith("/api/timeline/slots")) return reply(200, { ok: true, slots: [] });
    return reply(200, { ok: true });
  }));
  vi.stubGlobal("alert", vi.fn());
  vi.stubGlobal("confirm", vi.fn((message: string) => {
    confirms.push(message);
    return true;
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function clickGenerateVideo() {
  render(<Page />);
  const button = await screen.findByRole("button", { name: /generate video/i });
  fireEvent.click(button);
  return button;
}

const generateVideoPosts = () =>
  posts.filter((p) => p.url.includes("/generate_video"));

test("the confirm names what this generation costs, and what it is for", async () => {
  await clickGenerateVideo();
  await waitFor(() => expect(confirms.length).toBe(1));

  const asked = confirms[0];
  expect(asked).toContain("$3.20");
  // The inputs, not just the total: a human deciding whether $3.20 is worth it
  // needs to know it is 8 seconds of veo with the audio track turned on, which
  // is the toggle that doubled it.
  expect(asked).toContain("veo_3_1");
  expect(asked).toContain("8s");
  expect(asked).toContain("with audio");
});

test("the price the human confirmed is the price the request carries", async () => {
  await clickGenerateVideo();
  await waitFor(() => expect(generateVideoPosts().length).toBe(1));

  expect(generateVideoPosts()[0].body.accepted_cost).toBe(3.2);
});

test("declining the price generates nothing", async () => {
  vi.stubGlobal("confirm", vi.fn(() => false));
  await clickGenerateVideo();
  await waitFor(() => expect(screen.queryByText(/Loading Workspace/i)).toBeNull());

  expect(generateVideoPosts()).toEqual([]);
});

test("a price the studio could not fetch generates nothing", async () => {
  // THE DEFECT'S SHAPE, one level up: an unpriced request must not be sent at
  // all. Falling back to posting without a price would put the studio straight
  // back where it started, and the server would refuse it anyway — so the
  // honest thing is to say the quote failed rather than to try the spend.
  quoteReachable = false;
  await clickGenerateVideo();
  await waitFor(() => expect((globalThis.alert as any).mock.calls.length).toBe(1));

  expect(generateVideoPosts()).toEqual([]);
  expect(confirms).toEqual([]);
});
