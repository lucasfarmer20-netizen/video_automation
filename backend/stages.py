"""The six-stage FilmCraft spine, computed server-side.

SCRIPT -> DIRECT -> GENERATE -> ROUGH CUT -> REFINE -> EXPORT

This module is the single authority for which stage the film is in, which
stages are finished, which are blocked and why, and what the next meaningful
action is. The frontend renders what this returns; it does not decide any of it.

That split is a hard requirement, not a preference. The contract's §11.4 says
the UI must never claim approved / generated / saved / exported unless
authoritative backend state agrees, and the previous step header derived its
gating from counts the client had assembled itself. A second copy of a gating
rule in the browser is the same failure mode the shot-tier triage comment in
``director.py`` already warns about: two registries that quietly disagree.

Deliberately **not** global-aware: everything is computed from the storyboard
and the path bundle handed in. Nothing here reads ``config.MANIFEST_PATH`` or
asks which project is "active", so this module already satisfies the
per-request project identity that the next slice introduces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .manifest import Storyboard

StageId = Literal["script", "direct", "generate", "roughcut", "refine", "export"]

STAGE_ORDER: tuple[StageId, ...] = (
    "script", "direct", "generate", "roughcut", "refine", "export",
)

STAGE_NAMES: dict[StageId, str] = {
    "script": "Script",
    "direct": "Direct",
    "generate": "Generate",
    "roughcut": "Rough Cut",
    "refine": "Refine",
    "export": "Export",
}

# Primary CTA per stage, fixed by contract §2.1. One primary action per stage —
# competing primary flows are what the spine exists to prevent.
STAGE_CTA: dict[StageId, tuple[str, str]] = {
    "script":   ("Continue to Direct",  "goto:direct"),
    "direct":   ("Approve Beat Coverage", "approve:coverage"),
    "generate": ("Build Draft 1",       "build:draft1"),
    "roughcut": ("Continue to Refine",  "goto:refine"),
    "refine":   ("Continue to Export",  "goto:export"),
    "export":   ("Export Final Master", "export:master"),
}

# What each stage is authoritative for (contract §2.2). Carried in the payload
# so the UI can tell the user where a decision belongs instead of guessing.
STAGE_OWNS: dict[StageId, str] = {
    "script": "script text, scene boundaries, narration text and timing, narrator selection",
    "direct": "visual intent, beat coverage, sub-shots, shot type, intended duration, approval",
    "generate": "references, model execution, attempts, retries, takes, selected output, paid spend",
    "roughcut": "chronological edit, clip placement, trims, VO/music/SFX placement",
    "refine": "targeted fixes, issue review, non-destructive polish",
    "export": "final validation, version naming, frozen snapshot, master and FCPXML",
}

COMPLETE = "complete"
CURRENT = "current"
AVAILABLE = "available"
BLOCKED = "blocked"


@dataclass
class Stage:
    id: str
    name: str
    status: str = AVAILABLE
    blocked_reason: str = ""
    hint: str = ""
    cta: str = ""
    cta_action: str = ""
    owns: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "hint": self.hint,
            "cta": self.cta,
            "cta_action": self.cta_action,
            "owns": self.owns,
        }


@dataclass
class StageCounts:
    """Authoritative per-stage progress, counted from disk and the manifest."""

    beats: int = 0
    stills: int = 0
    narration: int = 0
    sfx: int = 0
    rendered: int = 0
    planned: int = 0        # beats with a saved coverage plan
    locked: int = 0         # beats whose coverage is locked
    exports: list[str] = field(default_factory=list)
    has_draft: bool = False

    def to_dict(self) -> dict:
        return {
            "beats": self.beats, "stills": self.stills, "narration": self.narration,
            "sfx": self.sfx, "rendered": self.rendered, "planned": self.planned,
            "locked": self.locked, "exports": list(self.exports),
            "has_draft": self.has_draft,
        }


def _stems(d: Path) -> set[str]:
    try:
        return {p.stem for p in d.iterdir() if p.is_file()}
    except (OSError, FileNotFoundError):
        return set()


def count(sb: Storyboard, paths: dict, plan_status=None) -> StageCounts:
    """Count real state. ``paths`` is an ``episode_paths()``-shaped bundle.

    ``plan_status`` is an optional ``beat_id -> str | None`` callable (normally
    ``director.load_plan(...).status``), injected rather than imported so this
    module stays free of the director's on-disk layout — and so a caller that
    already has plans in hand does not pay to re-read them.
    """
    render_dir = Path(paths["render"])
    rendered = _stems(render_dir)
    narration = _stems(Path(paths["narration"]))
    sfx = _stems(Path(paths["sfx"]))

    c = StageCounts(
        beats=len(sb.shots),
        stills=sum(1 for s in sb.shots if s.draft_image),
        narration=sum(1 for s in sb.shots if s.scene_id in narration),
        sfx=sum(1 for s in sb.shots if s.scene_id in sfx),
        rendered=sum(1 for s in sb.shots if s.scene_id in rendered),
    )

    if plan_status is not None:
        for s in sb.shots:
            st = plan_status(s.scene_id)
            if st:
                c.planned += 1
                if st in ("locked", "compiling", "compiled"):
                    c.locked += 1

    # Draft 1 is the assembled preview. "_preview" is excluded from the beat
    # counts above precisely so it can be tested for here.
    c.has_draft = (render_dir / "_preview.mp4").is_file()

    base = Path(paths["render"]).parent
    slug = paths.get("slug", "")
    c.exports = [kind for kind in ("fcpxml", "otio")
                 if slug and (base / f"{slug}.{kind}").is_file()]
    return c


def compute(sb: Storyboard, counts: StageCounts) -> list[Stage]:
    """The six stages with status, blocking reason, hint and primary CTA."""
    beats = counts.beats
    approved = bool(getattr(sb, "storyboard_approved", False))

    def mk(sid: StageId, *, complete: bool, blocked: str = "", hint: str = "") -> Stage:
        cta, action = STAGE_CTA[sid]
        return Stage(
            id=sid, name=STAGE_NAMES[sid],
            status=COMPLETE if complete else (BLOCKED if blocked else AVAILABLE),
            blocked_reason=blocked, hint=hint, cta=cta, cta_action=action,
            owns=STAGE_OWNS[sid],
        )

    stages: list[Stage] = []

    # --- Script ---------------------------------------------------------------
    stages.append(mk(
        "script",
        complete=bool(sb.script_locked and beats),
        hint=(f"{beats} beat{'' if beats == 1 else 's'} · "
              f"{counts.narration}/{beats} voiced" if beats else "no beats yet"),
    ))

    # --- Direct ---------------------------------------------------------------
    # Approval is Gate 1 and it is what Direct exists to produce, so Direct is
    # complete when the storyboard is approved -- not when plans merely exist.
    direct_blocked = "" if sb.script_locked else (
        "The script is not locked. Direct plans coverage against locked narration "
        "timing, so locking comes first."
    )
    stages.append(mk(
        "direct",
        complete=bool(approved and beats),
        blocked=direct_blocked,
        hint=(f"{counts.locked}/{beats} beats with locked coverage"
              if beats else "no beats to plan"),
    ))

    # --- Generate -------------------------------------------------------------
    # Gate 1, per CLAUDE.md: the paid tier is unreachable until the storyboard is
    # approved, because approval is where the render budget is allocated.
    gen_blocked = "" if approved else (
        "The storyboard is not approved. Approve coverage in Direct first — that "
        "is where the render budget is allocated, and no paid generation runs "
        "before it."
    )
    stages.append(mk(
        "generate",
        complete=bool(beats and counts.rendered >= beats),
        blocked=gen_blocked,
        hint=(f"{counts.rendered}/{beats} visuals ready" if beats else "no beats"),
    ))

    # --- Rough Cut ------------------------------------------------------------
    # Buildable before generation finishes: §6.2 is explicit that Draft 1 may use
    # placeholders. What it cannot do is build from nothing.
    rough_blocked = "" if counts.rendered else (
        "No visuals are ready yet. Generate at least one shot — Draft 1 can fill "
        "the rest with placeholders."
    )
    missing = max(0, beats - counts.rendered)
    stages.append(mk(
        "roughcut",
        complete=counts.has_draft,
        blocked=rough_blocked,
        hint=(f"Draft 1 built" if counts.has_draft
              else f"{counts.rendered}/{beats} ready"
                   + (f" · {missing} placeholder{'' if missing == 1 else 's'}"
                      if missing else "")),
    ))

    # --- Refine ---------------------------------------------------------------
    # Never reports "complete". Refine is optional polish (§8: optional polish
    # must not block export), so claiming completion would assert a review that
    # nobody performed. It stays available until the issue model lands.
    stages.append(mk(
        "refine",
        complete=False,
        blocked="" if counts.has_draft else "There is no Draft 1 to refine yet.",
        hint="targeted fixes" if counts.has_draft else "",
    ))

    # --- Export ---------------------------------------------------------------
    stages.append(mk(
        "export",
        complete=bool(counts.exports),
        blocked="" if counts.has_draft else "There is no cut to export yet.",
        hint=(", ".join(sorted(counts.exports)) if counts.exports else "nothing exported"),
    ))

    # The current stage is the earliest one that is neither finished nor blocked.
    for st in stages:
        if st.status == AVAILABLE:
            st.status = CURRENT
            break

    return stages


def payload(sb: Storyboard, paths: dict, plan_status=None) -> dict:
    """Everything the stage header needs, in one response."""
    counts = count(sb, paths, plan_status)
    stages = compute(sb, counts)
    current = next((s for s in stages if s.status == CURRENT), None)
    # Nothing is current when every stage is finished or blocked; the next
    # meaningful action is then the first blocked stage's way forward.
    blocked_first = next((s for s in stages if s.status == BLOCKED), None)
    return {
        "stages": [s.to_dict() for s in stages],
        "counts": counts.to_dict(),
        "current": current.id if current else None,
        "next_action": {
            "stage": current.id if current else (blocked_first.id if blocked_first else None),
            "label": current.cta if current else "",
            "action": current.cta_action if current else "",
            "blocked_reason": "" if current else (blocked_first.blocked_reason if blocked_first else ""),
        },
    }
