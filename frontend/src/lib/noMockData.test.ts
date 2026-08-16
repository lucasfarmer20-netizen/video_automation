/**
 * No production screen may render a fixture.
 *
 * `MOCK_SCENES` — whose own comment reads "Real beat ID mock constant for unit
 * testing / UI preview if needed" — was wired into two production render sites.
 * A human working on the Saugus Iron Works was shown:
 *
 *     FILM COVERAGE OVERVIEW
 *     1:12 (1 Scenes) | 0/1 Scenes Ready | 2 Warnings | Est. Cost: $3.82
 *     s004 — The Mountain Takes Its Toll | Draft | 1:12 | 11 shots | $3.82
 *
 * Every figure is the fixture: duration 72 → "1:12", shots_count 11, warnings 2,
 * estimated_cost 3.82. One of them is a PRICE — a quote for work that does not
 * exist, on a film that is not theirs, while their own locked coverage was not
 * on the screen at all.
 *
 * This is a scan, not an assertion about one panel, deliberately: the constant
 * was imported in two files and a fix confined to one of them leaves the class
 * open. It fails on any NEW production file that reaches for it, and it fails
 * just as loudly when a known offender is cleaned up and this list is not
 * tightened — so the exception cannot quietly become permanent.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";

const SRC = path.resolve(__dirname, "..");

/** Fixtures that must never reach a rendered screen. */
const FIXTURE_EXPORTS = ["MOCK_SCENES"];

/**
 * Production files still importing a fixture, each with its owner.
 *
 * `DirectorWorkspace.tsx` is being fixed in a separate session (-22) and is not
 * this PR's to touch; it is recorded here rather than excluded silently, so the
 * debt is visible and counted. When that lands, this map must go empty — and
 * the test below fails until it does, which is the point.
 */
const KNOWN_OFFENDERS: Record<string, string> = {
  "components/DirectorWorkspace.tsx":
    "routed to session -22; renders MOCK_SCENES into CompactMontageMatrix",
};

/** Every .ts/.tsx under src/ that ships to users (tests and fixtures excluded). */
function productionFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules") continue;
      productionFiles(full, acc);
      continue;
    }
    if (!/\.tsx?$/.test(entry.name)) continue;
    if (/\.test\.tsx?$/.test(entry.name)) continue;
    acc.push(full);
  }
  return acc;
}

const rel = (file: string) => path.relative(SRC, file).split(path.sep).join("/");

/**
 * Source with comments removed.
 *
 * Naming the fixture in a comment is how the next reader learns why this rule
 * exists — the explanations in `page.tsx` and `filmCoverage.ts` are the record
 * of what went wrong. A scan that cannot tell prose from code would force those
 * explanations to be deleted, which is the wrong direction entirely. The `:`
 * guard keeps `http://` in a string from swallowing the rest of its line.
 */
function codeOnly(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

/** Files whose CODE names a fixture, other than the module that defines it. */
function offenders(): string[] {
  return productionFiles(SRC)
    .filter((file) => rel(file) !== "lib/directorApi.ts")
    .filter((file) => {
      const code = codeOnly(fs.readFileSync(file, "utf8"));
      return FIXTURE_EXPORTS.some((name) => new RegExp(`\\b${name}\\b`).test(code));
    })
    .map(rel)
    .sort();
}

describe("fixtures never reach a production screen", () => {
  test("no production file imports a fixture except the known, tracked ones", () => {
    // Exact equality in both directions. A new file reaching for the fixture
    // fails this; so does a known offender being fixed without tightening the
    // list, which is what keeps the exception from becoming permanent.
    expect(offenders()).toEqual(Object.keys(KNOWN_OFFENDERS).sort());
  });

  test("the film coverage overview is not one of them", () => {
    // The screen the human was actually shown. Named separately from the scan
    // so a regression here reads as itself rather than as a list mismatch.
    const page = codeOnly(fs.readFileSync(path.join(SRC, "app/page.tsx"), "utf8"));
    FIXTURE_EXPORTS.forEach((name) =>
      expect(page).not.toMatch(new RegExp(`\\b${name}\\b`))
    );
  });

  test("the default selected beat is not a literal beat id", () => {
    // `useState<string>("s004")` — the fixture's own scene_id. The workspace
    // then asked for a plan for a beat that does not exist in the user's film
    // and correctly answered "No coverage plan found", which read as the
    // Director being broken.
    const page = fs.readFileSync(path.join(SRC, "app/page.tsx"), "utf8");
    expect(page).not.toMatch(/useState<string>\(\s*["']s\d{3}["']\s*\)/);
    expect(page).toContain("defaultSelectedBeat");
  });

  test("every known offender is real, so the list cannot rot", () => {
    // A stale entry would silently weaken the scan above.
    const present = new Set(offenders());
    Object.keys(KNOWN_OFFENDERS).forEach((file) => {
      expect(
        present.has(file),
        `${file} is listed as a known offender but no longer imports a fixture — ` +
          `remove it from KNOWN_OFFENDERS`
      ).toBe(true);
    });
  });
});
