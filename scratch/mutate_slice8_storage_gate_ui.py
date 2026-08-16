"""Mutation harness for V1 slice 8 — the storage gate ON SCREEN.

    python scratch/mutate_slice8_storage_gate_ui.py

The backend harness (``mutate_slice8_storage_gate.py``) proves the server stops
substituting. This one proves the refusal LANDS somewhere honest, which is a
separate claim and the one that was easiest to get wrong: failing closed without
a screen for it is not a smaller defect than the one it replaces, it is a
different one. Before this slice the 503 fell through to "No Active Project
Loaded", whose only call to action is **Initialize Project Workspace** — an
offer to seed a fresh manifest over a film that exists and is merely unreachable.

Same two obligations as every fix here: a mutation that reproduces the defect,
and a test that dies under a faithful mutation of the fix.

Probes exist for the same reason they exist on the money side. A red vitest run
proves the tests are sensitive to *something*. What has to be demonstrated is
the behaviour by name — that under the mutation the destructive button really is
back on the screen. The probe spec is written into ``src/`` for the duration of
one run (vitest's ``include`` is ``src/**/*.test.tsx``) and removed in a
``finally``, exactly like the file mutations.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PAGE = FRONTEND / "src" / "app" / "page.tsx"
PROBE_SPEC = FRONTEND / "src" / "app" / "zz_storage_probe.test.tsx"


# --------------------------------------------------------------------------- #
# probe: render the real page against a real storage-gate 503 and report what
# is actually on screen. It never asserts, so it stays green under every
# mutation and reports rather than judges.
# --------------------------------------------------------------------------- #

PROBE_SOURCE = r'''
import React from "react";
import { afterEach, beforeEach, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

const GATE_MESSAGE = "PROBE-GATE-SENTENCE";

const reply = (status: number, body: any) => ({
  ok: status < 400,
  status,
  headers: new Headers({ "X-Project-Id": "leshy" }),
  json: async () => body,
  text: async () => JSON.stringify(body),
}) as unknown as Response;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo) => {
    const url = String(input).replace("http://localhost:5000", "");
    if (url.startsWith("/api/project/active")) {
      return reply(503, {
        detail: { error: GATE_MESSAGE, storage_gate: "unavailable", project_id: "leshy" },
      });
    }
    if (url.startsWith("/api/projects")) {
      return reply(200, { ok: true, projects: [] });
    }
    if (url.startsWith("/api/assemble/status")) return reply(200, { ok: true, jobs: {} });
    return reply(200, { ok: true });
  }));
  vi.stubGlobal("alert", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("probe: what the studio shows when the durable store cannot answer", async () => {
  render(<Page />);
  await waitFor(() => {
    if (screen.queryByText(/Loading Workspace/i)) throw new Error("still loading");
  });

  const create = screen.queryByRole("button", { name: /initialize project workspace/i });
  console.log("PROBE_UI_CREATE_BUTTON=" + (create ? "shown" : "absent"));

  const gate = screen.queryByTestId("storage-gate-block");
  const noproj = screen.queryByText(/No Active Project Loaded/i);
  console.log("PROBE_UI_SCREEN=" + (gate ? "storage-gate" : noproj ? "no-active-project" : "other"));

  const body = document.body.textContent || "";
  console.log("PROBE_UI_SERVER_SENTENCE=" + (body.includes(GATE_MESSAGE) ? "shown" : "absent"));
  console.log("PROBE_UI_INTACT_LINE="
    + (/Nothing has been lost and nothing has been changed/i.test(body) ? "shown" : "absent"));
  console.log("PROBE_UI_RETRY="
    + (screen.queryByRole("button", { name: /try again/i }) ? "shown" : "absent"));
});
'''


@dataclass
class Mutation:
    name: str
    removes: str
    edits: list[tuple[Path, str, str]]
    probes: list[str] = field(default_factory=list)   # signatures, all from the one probe
    defect: bool = False
    expect: list[str] = field(default_factory=list)


# --- anchors ----------------------------------------------------------------------

INSTALL_BLOCK = (
    "      const block = storageGateBlock(data);\n"
    "      setStorageBlock(block);\n"
    "      if (block) return false;"
)

GATE_DETAIL_CHECK = '  if (data?.detail?.storage_gate !== "unavailable") return null;'
GATE_FALLBACK = (
    '  return String(data?.detail?.error || "The durable store could not be reached.");'
)

RENDER_GUARD = "  if (storageBlock) {"

INTACT_LINE = (
    '            <span className="text-zinc-400">Nothing has been lost and nothing has been changed.</span>{" "}'
)

RETRY_BUTTON = (
    "          <button\n"
    "            onClick={() => whileLoading(fetchActiveProject)}\n"
    "            className=\"w-full py-2.5 bg-zinc-100 hover:bg-white text-zinc-950 font-bold text-xs rounded-xl shadow-lg transition\"\n"
    "          >\n"
    "            Try again\n"
    "          </button>"
)

SERVER_SENTENCE = "            {storageBlock}"


MUTATIONS = [
    # ---- reproduction of the defect this screen exists to remove ----------------
    Mutation(
        "DEFECT: the refusal falls through to \"No Active Project Loaded\"",
        "the whole screen — the studio reads the 503 as 'no project', and offers "
        "to initialise a fresh workspace over a film that is merely unreachable",
        [(PAGE, INSTALL_BLOCK, "      // MUTANT: gate not read")],
        probes=["PROBE_UI_CREATE_BUTTON=shown",
                "PROBE_UI_SCREEN=no-active-project",
                "PROBE_UI_SERVER_SENTENCE=absent"],
        defect=True,
        expect=["the studio never offers to create a project over one it cannot reach",
                "the server's own sentence reaches the user, not a stand-in for it",
                "Try again re-reads, and the studio comes back when the store does"],
    ),
    Mutation(
        "DEFECT: the block is rendered nowhere, though it is read",
        "the render branch only — state is set and then ignored, which is the "
        "same screen by a different route",
        [(PAGE, RENDER_GUARD, "  if (false) {  // MUTANT")],
        probes=["PROBE_UI_CREATE_BUTTON=shown",
                "PROBE_UI_SCREEN=no-active-project"],
        defect=True,
        expect=["the studio never offers to create a project over one it cannot reach"],
    ),

    # ---- faithful mutations of the fix ------------------------------------------
    Mutation(
        "any refusal is read as the storage gate",
        "the STATED cause — a proxy or cold-start failure says nothing about the "
        "durable store, and inferring it is the same collapse the backend fix "
        "removes, rebuilt on the client",
        [(PAGE, GATE_DETAIL_CHECK, "  if (false) return null;  // MUTANT")],
        probes=[],   # invisible to the probe: the gate case itself is unchanged,
                     # so the evidence is the named test below.
        expect=["a 503 that does not name the storage gate is not treated as one",
                "an unknown project still reads as an unknown project"],
    ),
    Mutation(
        "a gate refusal with no sentence shows the word \"undefined\"",
        "the message fallback — a reply that names the gate but carries no "
        "prose renders the literal string \"undefined\" where the explanation "
        "goes, which tells the user nothing and reads as a studio bug",
        [(PAGE, GATE_FALLBACK,
          "  return String(data.detail.error);  // MUTANT")],
        probes=[],
        expect=["a refusal that names the gate but carries no sentence still blocks"],
    ),
    Mutation(
        "the screen paraphrases instead of quoting the server",
        "the operator's only pointer to WHICH store failed and why",
        [(PAGE, SERVER_SENTENCE, "            {\"Storage error.\"}  /* MUTANT */")],
        probes=["PROBE_UI_SERVER_SENTENCE=absent"],
        expect=["the server's own sentence reaches the user, not a stand-in for it"],
    ),
    Mutation(
        "the screen stops saying the work is intact",
        "the answer to the question this screen raises — a user told their "
        "storage is unavailable and nothing else assumes the worst",
        [(PAGE, INTACT_LINE, "            {/* MUTANT */}")],
        probes=["PROBE_UI_INTACT_LINE=absent"],
        expect=["it says the work is intact, because that is the question this raises"],
    ),
    Mutation(
        "the block offers no way past itself",
        "recovery — 503 is retryable by definition, and a screen that states a "
        "temporary block with no retry strands the user on a page reload",
        [(PAGE, RETRY_BUTTON, "          {/* MUTANT: no retry */}")],
        probes=["PROBE_UI_RETRY=absent"],
        expect=["Try again re-reads, and the studio comes back when the store does"],
    ),
]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
_NPX = "npx.cmd" if os.name == "nt" else "npx"


def _vitest(*args: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [_NPX, "vitest", "run", *args], cwd=FRONTEND, env=_ENV,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_suite() -> tuple[bool, str]:
    return _vitest()


def run_probe() -> tuple[bool, str]:
    # `--disable-console-intercept` is required: vitest 4 swallows console.log
    # from a passing test, and a probe whose output is silently dropped reports
    # every signature as MISSING -- a harness fault dressed as a result.
    PROBE_SPEC.write_text(PROBE_SOURCE, encoding="utf-8")
    try:
        return _vitest("--disable-console-intercept",
                       str(PROBE_SPEC.relative_to(FRONTEND)).replace("\\", "/"))
    finally:
        PROBE_SPEC.unlink(missing_ok=True)


def probe_lines(out: str) -> list[str]:
    lines = []
    for raw in out.splitlines():
        # vitest prefixes console output with the spec path; take the token.
        for token in raw.split():
            if token.startswith("PROBE_UI_"):
                lines.append(token)
    return lines


def shows(out: str, signature: str) -> bool:
    return signature in probe_lines(out)


def failing_tests(out: str) -> list[str]:
    """Test names vitest reported as failing.

    vitest prints ``× <file> > <describe> > <test>`` per failure; the trailing
    segment is the test name, which is what the mutations name.
    """
    names = set()
    for line in out.splitlines():
        stripped = line.strip()
        for marker in ("×", "FAIL"):
            if stripped.startswith(marker):
                names.add(stripped)
    return sorted(names)


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def apply(path: Path, find: str, replace: str) -> None:
    text = _read(path)
    if "\r\n" in text:
        find, replace = find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    hits = text.count(find)
    if hits == 0:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    if hits > 1:
        raise SystemExit(f"anchor matches {hits} sites in {path.name} — narrow it:\n{find}")
    _write(path, text.replace(find, replace, 1))


def digest(paths) -> dict:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    if not (FRONTEND / "node_modules").is_dir():
        print("frontend/node_modules is absent — run `npm ci` in frontend/ first.")
        return 1

    touched = sorted({p for m in MUTATIONS for p, _, _ in m.edits})
    pristine = {p: _read(p) for p in touched}
    before = digest(touched)

    ok, out = run_suite()
    print(f"baseline suite: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out[-4000:])
        return 1

    pok, pout = run_probe()
    print(f"baseline probe: {'ran' if pok else 'ERRORED'}")
    for ln in probe_lines(pout):
        print(f"    {ln}")
    if not pok:
        print(pout[-2500:])
        return 1
    clean_probe = pout

    stale = [f"{m.name}: {sig} is present PRISTINE"
             for m in MUTATIONS for sig in m.probes if shows(clean_probe, sig)]

    survived, unproven = [], list(stale)
    for mut in MUTATIONS:
        snapshot = {p: _read(p) for p, _, _ in mut.edits}
        probe_out = ""
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            suite_ok, suite_out = run_suite()
            if mut.probes:
                probe_out = run_probe()[1]
        finally:
            for path, text in snapshot.items():
                _write(path, text)

        killed = not suite_ok
        print(f"\n{mut.name}")
        print(f"    removes: {mut.removes}")
        print(f"    suite: {'killed' if killed else 'SURVIVED'}")
        failed = failing_tests(suite_out)
        for t in failed[:8]:
            print(f"      x {t}")
        if not killed:
            survived.append(mut.name)

        for want in mut.expect:
            if not any(want in t for t in failed):
                unproven.append(f"{mut.name}: expected \"{want}\" to fail")

        if mut.probes:
            print("    probe:")
            for ln in probe_lines(probe_out):
                print(f"      {ln}")
            for sig in mut.probes:
                shown = shows(probe_out, sig)
                print(f"    signature {'SHOWN' if shown else 'MISSING'}: {sig}")
                if not shown:
                    unproven.append(
                        f"{mut.name}: probe did NOT show {sig} — killed, but the "
                        f"behaviour it names was not demonstrated")

    after = digest(touched)
    dirty = [p.name for p in touched if before[p] != after[p]]
    for p in touched:
        if before[p] != after[p]:
            _write(p, pristine[p])
    PROBE_SPEC.unlink(missing_ok=True)

    ok, _ = run_suite()
    print(f"\nrestored suite: {'PASS' if ok else 'FAIL'}")
    print(f"mutations: {len(MUTATIONS)} defined, {len(survived)} survived, "
          f"{len(MUTATIONS) - len(survived)} killed")
    if dirty:
        print(f"  TREE NOT RESTORED — was still modified: {', '.join(dirty)}")
    if survived:
        print("\nmutations that SURVIVED (unprotected safeguards):")
        for name in survived:
            print(f"  - {name}")
    if unproven:
        print("\nnot proven:")
        for line in unproven:
            print(f"  - {line}")
    return 1 if survived or unproven or dirty or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
