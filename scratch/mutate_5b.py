"""Mutation harness for slice 5b's frontend tests.

Guardrails require a test that fails under a faithful mutation of the change,
plus a mutation that reproduces the original defect. Each entry below is applied
to the working tree, the suite is run, and the file is restored — so the run
reports both directions: mutated (must fail) and restored (must pass).

    python scratch/mutate_5b.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SLOTS = FRONTEND / "src" / "lib" / "slots.ts"
TIMELINE = FRONTEND / "src" / "components" / "MultitrackTimeline.tsx"

# (name, contract clause, file, find, replace)
MUTATIONS = [
    (
        "placeholder re-derived from media client-side",
        "§11.4",
        SLOTS,
        "  return slot.placeholder === false;",
        "  return !!slot.media;",
    ),
    (
        "placeholder inferred from whether the frame loaded",
        "§11.4",
        TIMELINE,
        "                const filled = isFilled(slot);",
        "                const filled = isFilled(slot) && !brokenMedia.has(slot.id);",
    ),
    (
        "V1 clips keyed on array position",
        "§7.1",
        TIMELINE,
        "              ) : slotBlocks.map(({ slot, start, dur: laidOut }: SlotBlock) => {",
        "              ) : slotBlocks.map(({ slot, start, dur: laidOut }: SlotBlock, _i: number) => {",
    ),
    (
        "selection held as a position instead of a slot id",
        "§7.1",
        TIMELINE,
        "                const isSel = selectedSlotId === slot.id;",
        "                const isSel = selectedSlotId === String(slot.index);",
    ),
    (
        "coverage counted in the component instead of rendered",
        "§6.2",
        TIMELINE,
        "              {coverage.summary}",
        "              {`${(slots || []).filter(isFilled).length}/${(slots || []).length}"
        " visuals ready`}",
    ),
    (
        "ORIGINAL DEFECT: V1 built from beats and their file paths",
        "§7.1",
        TIMELINE,
        "    () => layoutSlots(slots || [], beatStarts),",
        "    () => layoutSlots(shots.map((s, i) => ({\n"
        "      id: s.scene_id, beat_id: s.scene_id, shot_id: \"\", index: i,\n"
        "      intended_duration: s.camera?.duration || 0, expected_media: \"video\",\n"
        "      media: s.draft_image || \"\", source_attempt: \"\", trim_in: 0, trim_out: 0,\n"
        "      placeholder: !s.draft_image, duration: s.camera?.duration || 0,\n"
        "    })), beatStarts),",
    ),
]

# Extra text some mutations need in scope, applied alongside them.
EXTRAS = {
    "placeholder inferred from whether the frame loaded": (
        TIMELINE,
        "  const [selectedSlotId, setSelectedSlotId] = useState<string | null>(null);",
        "  const [selectedSlotId, setSelectedSlotId] = useState<string | null>(null);\n"
        "  const [brokenMedia, setBrokenMedia] = useState<Set<string>>(new Set());",
    ),
    "V1 clips keyed on array position": (
        TIMELINE,
        "                    key={slot.id}",
        "                    key={_i}",
    ),
    "selection held as a position instead of a slot id": (
        TIMELINE,
        "                      const next = isSel ? null : slot.id;",
        "                      const next = isSel ? null : String(slot.index);",
    ),
}

EXTRA_2 = {
    "placeholder inferred from whether the frame loaded": (
        TIMELINE,
        "onError={(e) => { e.currentTarget.style.visibility = \"hidden\"; }} />\n"
        "                        <div className=\"absolute inset-0 bg-gradient-to-t",
        "onError={(e) => { e.currentTarget.style.visibility = \"hidden\";\n"
        "                               setBrokenMedia((b) => new Set(b).add(slot.id)); }} />\n"
        "                        <div className=\"absolute inset-0 bg-gradient-to-t",
    ),
}


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        ["npm", "test", "--silent"], cwd=FRONTEND, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        shell=(sys.platform == "win32"),
    )
    out = proc.stdout + proc.stderr
    return proc.returncode == 0, out


def failing_tests(out: str) -> list[str]:
    return sorted({
        line.strip().lstrip("×").strip()
        for line in out.splitlines() if line.strip().startswith("×")
    })


def apply(path: Path, find: str, replace: str) -> str:
    text = path.read_text(encoding="utf-8")
    if find not in text:
        raise SystemExit(f"anchor not found in {path.name}:\n{find}")
    path.write_text(text.replace(find, replace, 1), encoding="utf-8")
    return text


def main() -> int:
    ok, out = run_suite()
    print(f"baseline: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out)
        return 1

    bad = []
    for name, clause, path, find, replace in MUTATIONS:
        originals: list[tuple[Path, str]] = [(path, path.read_text(encoding="utf-8"))]
        try:
            apply(path, find, replace)
            for table in (EXTRAS, EXTRA_2):
                if name in table:
                    p2, f2, r2 = table[name]
                    apply(p2, f2, r2)
            ok, out = run_suite()
        finally:
            for p, text in originals:
                p.write_text(text, encoding="utf-8")

        verdict = "killed" if not ok else "SURVIVED"
        if ok:
            bad.append(name)
        print(f"\n{clause} {name}: {verdict}")
        for t in failing_tests(out):
            print(f"    × {t}")

    ok, _ = run_suite()
    print(f"\nrestored: {'PASS' if ok else 'FAIL'}")
    if bad:
        print("\nmutations that survived:")
        for name in bad:
            print(f"  - {name}")
    return 1 if bad or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
