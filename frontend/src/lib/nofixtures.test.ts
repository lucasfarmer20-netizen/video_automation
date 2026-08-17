/**
 * No production module may reach for a test fixture.
 *
 * `MOCK_SCENES` is a hardcoded film in `directorApi.ts` — "s004 — The Mountain
 * Takes Its Toll", 11 shots, `estimated_cost: 3.82` — whose own comment says it
 * is "for unit testing / UI preview". It reached production twice, in two
 * different components, and in both it put a fabricated cost in front of a human
 * who was about to spend money.
 *
 * `DirectorWorkspace.nomock.test.tsx` proves it is gone from the one view it can
 * render. This proves it about paths a jsdom test never reaches: a mode nobody
 * clicks, a branch behind a feature flag, a component added next month. Rendering
 * catches the instance; reading the source catches the class.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, test } from "vitest";

const SRC = join(__dirname, "..");

/**
 * Exports that exist to be rendered by tests, never by the product.
 *
 * The match is a plain substring over the whole file, comments included. That is
 * deliberate: a scanner that skips comments can be satisfied by moving a
 * reference into one, and "explaining" a fixture in a comment beside the code
 * that uses it is precisely how this survived review the first time. The cost is
 * that prose discussing the defect must avoid the literal name, which
 * `DirectorWorkspace.tsx` now does and says so.
 */
const FIXTURES = ["MOCK_SCENES"];

/**
 * Files allowed to name a fixture, each with the reason and its owner.
 *
 * This is not an amnesty. The test below asserts that every entry here is STILL
 * in breach — so the moment the listed file is fixed, this test fails and tells
 * whoever fixed it to delete the line. An exemption cannot outlive the defect it
 * was granted for, which is the failure mode of every allowlist that does not do
 * this.
 */
const KNOWN_BREACHES: Record<string, string> = {
  // FilmOverviewPanel, same defect, different component. Held by worker -21 in
  // the plan-scene presentation round; editing it here would collide.
  "app/page.tsx": "worker -21 owns page.tsx — remove this line once it is fixed",
};

/** Where the fixtures are allowed to be DEFINED. */
const DEFINITIONS = ["lib/directorApi.ts"];

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full));
      continue;
    }
    if (!/\.(ts|tsx)$/.test(entry)) continue;
    if (/\.test\.(ts|tsx)$/.test(entry)) continue; // tests may use fixtures
    out.push(full);
  }
  return out;
}

/** Every production file that names a fixture, as repo-relative paths. */
function breaches(): string[] {
  const found: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const rel = relative(SRC, file).replace(/\\/g, "/");
    if (DEFINITIONS.includes(rel)) continue;
    const text = readFileSync(file, "utf8");
    if (FIXTURES.some((f) => text.includes(f))) found.push(rel);
  }
  return found.sort();
}

describe("test fixtures stay out of production code", () => {
  test("no production module references a fixture, beyond the known breaches", () => {
    const unexpected = breaches().filter((f) => !(f in KNOWN_BREACHES));

    expect(unexpected, `these files render a test fixture to a human: ${unexpected}`)
      .toEqual([]);
  });

  test("the scan is real — it can see a fixture where one exists", () => {
    // Without this, a broken walker (wrong root, over-eager filter) reports an
    // empty list and the test above passes by finding nothing at all.
    const files = sourceFiles(SRC).map((f) => relative(SRC, f).replace(/\\/g, "/"));
    expect(files).toContain("components/DirectorWorkspace.tsx");
    expect(files).toContain("lib/directorApi.ts");
    expect(files.length).toBeGreaterThan(10);

    const defining = readFileSync(join(SRC, "lib/directorApi.ts"), "utf8");
    expect(FIXTURES.every((f) => defining.includes(f))).toBe(true);
  });

  test("every exemption is still in breach, or it should have been deleted", () => {
    // A self-retiring allowlist. When -21 lands their fix, this fails and names
    // the line to remove, rather than quietly widening the rule for ever.
    const current = breaches();
    for (const [file, why] of Object.entries(KNOWN_BREACHES)) {
      expect(
        current,
        `${file} no longer references a fixture — delete its KNOWN_BREACHES entry (${why})`
      ).toContain(file);
    }
  });

  test("DirectorWorkspace is not among the breaches", () => {
    // Named explicitly rather than left to the general rule: this is the file
    // the defect was fixed in, and the assertion should say so.
    expect(breaches()).not.toContain("components/DirectorWorkspace.tsx");
  });
});
