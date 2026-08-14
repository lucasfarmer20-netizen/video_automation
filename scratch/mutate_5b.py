"""Mutation harness for slice 5b's frontend tests.

Guardrails require a test that fails under a faithful mutation of the change,
plus a mutation that reproduces the original defect. Each mutation below is a
list of edits, applied together, the suite run, then every touched file
restored — so the run reports both directions: mutated (must fail) and restored
(must pass).

Restoration snapshots every path a mutation names, not just the first. An
earlier version snapshotted only the primary file and restored correctly by
accident, because every edit happened to target the same one; a cross-file
mutation would have left the tree dirty.

    python scratch/mutate_5b.py
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SLOTS = FRONTEND / "src" / "lib" / "slots.ts"
TIMELINE = FRONTEND / "src" / "components" / "MultitrackTimeline.tsx"
PAGE = FRONTEND / "src" / "app" / "page.tsx"


@dataclass
class Mutation:
    name: str
    clause: str
    edits: list[tuple[Path, str, str]]


MUTATIONS = [
    Mutation(
        "placeholder re-derived from media client-side", "§11.4",
        [(SLOTS,
          "  return slot.placeholder === false;",
          "  return !!slot.media;")],
    ),
    Mutation(
        "placeholder inferred from whether the frame loaded", "§11.4",
        [(TIMELINE,
          "  const [selectedSlotId, setSelectedSlotId] = useState<string | null>(null);",
          "  const [selectedSlotId, setSelectedSlotId] = useState<string | null>(null);\n"
          "  const [brokenMedia, setBrokenMedia] = useState<Set<string>>(new Set());"),
         (TIMELINE,
          "                const filled = isFilled(slot);",
          "                const filled = isFilled(slot) && !brokenMedia.has(slot.id);"),
         (TIMELINE,
          "                               onError={(e) => { e.currentTarget.style.visibility = \"hidden\"; }} />",
          "                               onError={(e) => { e.currentTarget.style.visibility = \"hidden\";\n"
          "                                 setBrokenMedia((b) => new Set(b).add(slot.id)); }} />")],
    ),
    Mutation(
        "V1 clips keyed on array position", "§7.1",
        [(TIMELINE,
          "              ) : slotBlocks.map(({ slot, start, dur: laidOut }: SlotBlock) => {",
          "              ) : slotBlocks.map(({ slot, start, dur: laidOut }: SlotBlock, _i: number) => {"),
         (TIMELINE,
          "                    key={slot.id}",
          "                    key={_i}")],
    ),
    Mutation(
        "selection held as a position instead of a slot id", "§7.1",
        [(TIMELINE,
          "                const isSel = selectedSlotId === slot.id;",
          "                const isSel = selectedSlotId === String(slot.index);"),
         (TIMELINE,
          "                      const next = isSel ? null : slot.id;",
          "                      const next = isSel ? null : String(slot.index);")],
    ),
    Mutation(
        "coverage counted in the component instead of rendered", "§6.2",
        [(TIMELINE,
          "              {coverage.summary}",
          "              {`${(slots || []).filter(isFilled).length}/${(slots || []).length}"
          " visuals ready`}")],
    ),
    Mutation(
        "slot media drawn into an <img>, which cannot decode a clip", "§7.1",
        [(TIMELINE,
          "                        <video src={`${mediaUrl(slot.media)}#t=0.1`} data-testid={`slot-media-${slot.id}`}",
          "                        <img src={`${mediaUrl(slot.media)}#t=0.1`} data-testid={`slot-media-${slot.id}`}")],
    ),
    Mutation(
        "a poster borrowed from whichever take is chosen now", "§11.4",
        [(TIMELINE,
          "                        <video src={`${mediaUrl(slot.media)}#t=0.1`} data-testid={`slot-media-${slot.id}`}",
          "                        <video src={`${mediaUrl(slot.media)}#t=0.1`} data-testid={`slot-media-${slot.id}`}\n"
          "                               poster={(() => {\n"
          "                                 const b = shots.find((s) => s.scene_id === slot.beat_id);\n"
          "                                 return b?.draft_image ? mediaUrl(b.draft_image) : undefined;\n"
          "                               })()}")],
    ),
    Mutation(
        "a trim refusal that outlives the slot it was about", "§11.4",
        [(TIMELINE,
          "                  setTrimError(null);\n"
          "                  const v = parseFloat(raw);",
          "                  const v = parseFloat(raw);"),
         (TIMELINE,
          "                    {trimError?.slotId === selectedSlot.id && (",
          "                    {trimError && (")],
    ),
    Mutation(
        "the sidebar's project id dropped on the way to the page", "§11.3",
        [(PAGE,
          "            onSelectProject={(rel, projectId) => {\n"
          "              handleSelectProject(rel, projectId);",
          "            onSelectProject={(rel) => {\n"
          "              handleSelectProject(rel);")],
    ),
    Mutation(
        "a held cut served for whichever film is on screen", "§11.4",
        [(SLOTS,
          "  return held.projectId === projectId ? held : NO_SLOT_VIEW;",
          "  return held;")],
    ),
    Mutation(
        "the chosen take reported as the one in the slot", "§11.4",
        [(TIMELINE,
          "Take {i + 1}{slotTakes.chosen === i ? \" · chosen\" : \"\"}",
          "Take {i + 1}{slotTakes.chosen === i ? \" · in use\" : \"\"}")],
    ),
    Mutation(
        "a trim the server would accept and the slot cannot survive", "§7.1",
        [(TIMELINE,
          "                  const bad = rejectTrim(t);\n"
          "                  if (bad) { setTrimError({ slotId: selectedSlot.id, why: bad }); return; }",
          "                  const bad: string | null = null;\n"
          "                  if (bad) { setTrimError({ slotId: selectedSlot.id, why: bad }); return; }")],
    ),
    Mutation(
        "an uncleared gate left for the job log to mention", "§6.2",
        [(TIMELINE,
          "              {draftGateNote && (",
          "              {false && draftGateNote && (")],
    ),
    Mutation(
        "duration fallback that ignores the trim the server prefers", "§7.1",
        [(SLOTS,
          "  if (slot.trim_out && slot.trim_out > slot.trim_in) {\n"
          "    return Math.round((slot.trim_out - slot.trim_in) * 1000) / 1000;\n"
          "  }\n",
          "")],
    ),
    Mutation(
        "ORIGINAL DEFECT: V1 built from beats and their file paths", "§7.1",
        [(TIMELINE,
          "    () => layoutSlots(slots || [], beatStarts),",
          "    () => layoutSlots(shots.map((s, i) => ({\n"
          "      id: s.scene_id, beat_id: s.scene_id, shot_id: \"\", index: i,\n"
          "      intended_duration: s.camera?.duration || 0, expected_media: \"video\",\n"
          "      media: s.draft_image || \"\", source_attempt: \"\", trim_in: 0, trim_out: 0,\n"
          "      placeholder: !s.draft_image, duration: s.camera?.duration || 0,\n"
          "    })), beatStarts),")],
    ),
]


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        ["npm", "test", "--silent"], cwd=FRONTEND, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        shell=(sys.platform == "win32"),
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def failing_tests(out: str) -> list[str]:
    return sorted({
        line.strip().lstrip("×").strip()
        for line in out.splitlines() if line.strip().startswith("×")
    })


def apply(path: Path, find: str, replace: str) -> None:
    text = path.read_text(encoding="utf-8")
    if find not in text:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    path.write_text(text.replace(find, replace, 1), encoding="utf-8")


def main() -> int:
    ok, out = run_suite()
    print(f"baseline: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out)
        return 1

    survived = []
    for mut in MUTATIONS:
        # Snapshot every file this mutation touches, before touching any of them.
        snapshot = {p: p.read_text(encoding="utf-8") for p, _, _ in mut.edits}
        try:
            for path, find, replace in mut.edits:
                apply(path, find, replace)
            ok, out = run_suite()
        finally:
            for path, text in snapshot.items():
                path.write_text(text, encoding="utf-8")

        if ok:
            survived.append(mut.name)
        print(f"\n{mut.clause} {mut.name}: {'SURVIVED' if ok else 'killed'}")
        for t in failing_tests(out):
            print(f"    x {t}")

    ok, _ = run_suite()
    print(f"\nrestored: {'PASS' if ok else 'FAIL'}")
    if survived:
        print("\nmutations that survived:")
        for name in survived:
            print(f"  - {name}")
    return 1 if survived or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
