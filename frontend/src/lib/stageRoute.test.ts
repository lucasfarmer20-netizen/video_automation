/**
 * The stage survives a reload, because it lives in the URL.
 *
 * `activeStage` was `useState<StageId>("script")` with no persistence and no
 * routing: every refresh landed on Script wherever the user had been. The human
 * who locked a coverage plan, refreshed, and reported "I don't see a way back
 * into the director after refresh" was describing exactly this — their plan was
 * safe on the server and the studio had no route back to it.
 */
import { describe, expect, test } from "vitest";
import {
  ROUTABLE_STAGES,
  hashAlreadyNames,
  hashForStage,
  isRoutableStage,
  stageFromHash,
} from "./stageRoute";

describe("reading the stage out of a URL", () => {
  test("every stage the header can show round-trips through the URL", () => {
    // The defect is a stage that cannot be returned to. If any one of them
    // fails to round-trip, that stage is unreachable after a refresh.
    ROUTABLE_STAGES.forEach((stage) => {
      expect(stageFromHash(hashForStage(stage))).toBe(stage);
    });
    // …and "direct" specifically, since that is the one that was reported.
    expect(stageFromHash("#direct")).toBe("direct");
  });

  test("a hash with no leading # is still read", () => {
    expect(stageFromHash("direct")).toBe("direct");
  });

  test("case and surrounding space do not lose the stage", () => {
    expect(stageFromHash("#Direct")).toBe("direct");
    expect(stageFromHash("  #direct  ")).toBe("direct");
  });

  test("an unrecognised fragment yields null rather than a guess", () => {
    // A hash from a future version, a typo, or an unrelated `#section` anchor
    // must leave the studio where it is instead of navigating somewhere it
    // invented — including, especially, not silently falling back to Script,
    // which is the behaviour being fixed.
    expect(stageFromHash("#storyboard")).toBeNull();
    expect(stageFromHash("#")).toBeNull();
    expect(stageFromHash("")).toBeNull();
    expect(stageFromHash("#direct-workspace")).toBeNull();
  });

  test("isRoutableStage accepts the six stages and nothing else", () => {
    expect(isRoutableStage("direct")).toBe(true);
    expect(isRoutableStage("export")).toBe(true);
    expect(isRoutableStage("storyboard")).toBe(false);
    expect(isRoutableStage("")).toBe(false);
  });
});

describe("writing the stage back to the URL", () => {
  test("the fragment names the stage", () => {
    expect(hashForStage("direct")).toBe("#direct");
  });

  test("a URL that already names the stage is recognised as such", () => {
    // Rewriting an identical URL on every render is how Back stops working.
    expect(hashAlreadyNames("#direct", "direct")).toBe(true);
    expect(hashAlreadyNames("#Direct", "direct")).toBe(true);
    expect(hashAlreadyNames("#script", "direct")).toBe(false);
    expect(hashAlreadyNames("", "direct")).toBe(false);
  });
});
