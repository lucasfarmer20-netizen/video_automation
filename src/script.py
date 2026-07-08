"""Script stage: Claude-drafted folklore narration + the script gate.

Calls the Anthropic API (Claude Opus 4.8) with an anti-AI-tell system prompt to
draft the narration *and* a matching storyboard beat list, writes it into
``storyboard_manifest.json`` for human refinement, and locks the script before
anything downstream (audio.py) may consume it.

Design note: the draft produces narration and the per-beat visual prompt
together so the two stay in sync; the human refines both at the gate. The API
call is isolated in ``generate_script`` — ``_beats_to_storyboard`` and
``lock_script`` are pure and unit-testable without a key.

CLI:
    python -m src.script --topic "The manananggal of Barrio Consuelo"
    python -m src.script --lock          # sets script_locked after you edit
"""

from __future__ import annotations

import argparse
import json
import os

import anthropic

from . import config
from .manifest import Camera, MotionType, Shot, Storyboard, load, save

# Strong writer by default; overridable without touching code.
DEFAULT_MODEL = os.environ.get("SCRIPT_MODEL", "claude-opus-4-8")

SYSTEM_PROMPT = """\
You are the head writer for "Deep Root Lore," a dark-folklore horror narration \
channel. You write voiceover scripts for atmospheric, ink-and-watercolor \
graphic-novel horror videos rooted in real world folklore (Philippine, Slavic, \
and other traditions).

Write narration that a human wrote, not narration that sounds "AI." That means:

- Vary sentence length hard. Real narration lurches — a long, breathing line, \
then two words. Then silence. Use fragments and unfinished thoughts for dread.
- Kill the tells: no tricolons ("the cold, the dark, the silence"), no \
"not X, but Y", no "Little did they know", no "stands as a testament", no \
sentences that just restate what you already showed.
- Concrete sensory detail over abstraction. Not "fear crept in" but "the salt \
didn't move. That's how she knew." One weird, true, specific detail per beat.
- Write for the ear. If a line is hard to say aloud, cut it.
- Respect the folklore. Ground it in real lore; don't invent generic monsters.

MONETIZATION SAFETY (hard rule — this is a YouTube channel):
- Imply, never show. No explicit gore, blood, viscera, wounds, or dismemberment \
in the narration or the visuals. Convey horror through shadow, silhouette, \
lighting, aftermath, suggestion, and sound. Cut away before the act.
- No graphic harm to children or infants shown or described. If the lore \
involves a child, handle it with heavy restraint or keep the child off-screen.
- Dread and suspense, not shock or disgust. No sexual content.
This keeps the video advertiser-friendly and out of age restriction. Restraint \
is the aesthetic here, not a limitation.

You also plan the storyboard. For each beat, give the narration and a matching \
visual, and propose how much motion it deserves. Motion tiers (cost matters — \
reserve the paid one for a handful of hero shots):
- "static": a still + subtle FX (candle flicker, drifting smoke). Cheap.
- "parallax": a 2.5D depth-parallax move on a still. Cheap. Most beats.
- "ai_video": true generated motion. Expensive — only genuine motion beats \
(a transformation, wings unfurling, a face turning). Use sparingly.

The visual for each beat is a prompt for an image model, in the Deep Root Lore \
style: heavy ink linework, watercolor washes, paper grain, chiaroscuro \
candlelight, near-black shadow with warm amber highlights, cinematic 16:9. \
Favor shadow-play/silhouette for the scariest reveals — it is cheap and always \
on-model. End every visual prompt with ", Deep Root Lore style".

Number beats s001, s002, ... in order."""

# JSON schema the model must fill. Enums keep motion/camera values manifest-valid.
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "narration": {"type": "string"},
                    "visual": {"type": "string"},
                    "suggested_motion_type": {
                        "type": "string",
                        "enum": ["static", "parallax", "ai_video"],
                    },
                    "suggested_camera": {
                        "type": "string",
                        "enum": ["push_in", "push_out", "pan_left", "pan_right", "static"],
                    },
                    "suggested_fx": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "scene_id",
                    "narration",
                    "visual",
                    "suggested_motion_type",
                    "suggested_camera",
                    "suggested_fx",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "beats"],
    "additionalProperties": False,
}


def _beats_to_storyboard(data: dict) -> Storyboard:
    """Pure mapping: model JSON -> Storyboard (script_locked stays False)."""
    shots: list[Shot] = []
    for i, beat in enumerate(data.get("beats", []), start=1):
        shots.append(
            Shot(
                scene_id=beat.get("scene_id") or f"s{i:03d}",
                narration=beat.get("narration", ""),
                prompt=beat.get("visual", ""),
                motion_type=MotionType(beat.get("suggested_motion_type", "parallax")),
                camera=Camera(move=beat.get("suggested_camera", "push_in")),
                fx=list(beat.get("suggested_fx") or []),
            )
        )
    return Storyboard(title=data.get("title", ""), script_locked=False, shots=shots)


def generate_script(
    topic: str,
    num_beats: int | None = None,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> Storyboard:
    """Draft narration + storyboard beats for ``topic`` via Claude."""
    config.require_for("script")  # fail loudly if ANTHROPIC_API_KEY is unset
    client = client or anthropic.Anthropic()

    if num_beats:
        scope = f"Produce about {num_beats} beats."
    else:
        scope = "Produce as many beats as the story needs (typically 15-40)."

    user_prompt = (
        "Write the narration script and storyboard beats for a Deep Root Lore "
        f"folklore horror episode.\n\nTopic / brief:\n{topic}\n\n{scope}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        messages=[{"role": "user", "content": user_prompt}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to draft this script (safety refusal).")

    text = next(b.text for b in response.content if b.type == "text")
    return _beats_to_storyboard(json.loads(text))


def lock_script(storyboard: Storyboard) -> Storyboard:
    """The script gate: mark the script locked once it is human-approved.

    Refuses to lock an empty storyboard or beats with no narration.
    """
    if not storyboard.shots:
        raise ValueError("Cannot lock: storyboard has no beats.")
    missing = [s.scene_id for s in storyboard.shots if not s.narration.strip()]
    if missing:
        raise ValueError(f"Cannot lock: beats missing narration: {missing}")
    storyboard.script_locked = True
    return storyboard


def _main() -> None:
    parser = argparse.ArgumentParser(description="Deep Root Lore script stage.")
    parser.add_argument("--topic", help="Episode topic / brief to draft from.")
    parser.add_argument("--beats", type=int, default=None, help="Approx beat count.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id.")
    parser.add_argument(
        "--lock",
        action="store_true",
        help="Lock the existing manifest's script (the script gate).",
    )
    args = parser.parse_args()

    if args.lock:
        sb = lock_script(load())
        save(sb)
        print(f"Script locked: {len(sb.shots)} beats. Ready for audio.py.")
        return

    if not args.topic:
        parser.error("provide --topic to draft, or --lock to lock the current script.")

    sb = generate_script(args.topic, num_beats=args.beats, model=args.model)
    save(sb)
    print(f'Drafted "{sb.title}" - {len(sb.shots)} beats -> {config.MANIFEST_PATH}')
    print("Refine the narration/visuals, then: python -m src.script --lock")


if __name__ == "__main__":
    _main()
