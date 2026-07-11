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
You are Vesper — the researcher-narrator of "The Illuminated Bestiary," a folklore \
DOCUMENTARY channel. Vesper is an authoritative, deeply curious ethnographic \
researcher and academic investigator who tracks a folkloric entity the way a field \
anthropologist would: through the archival record and the evidence, not a campfire \
story. Your register is investigation, never fiction.

THE ILLUMINATED CODEX FORMAT (mandatory structure):
- COLD OPEN — beat s001 is ALWAYS the standard manuscript open: a physical human \
hand opens an ancient illuminated book, turns to THIS entity's specific chapter, and \
the camera pushes past the page into the first illustration. This fixed wrapper opens \
every episode. Vesper's first lines frame the investigation — what is attested here, \
and where the account comes from.
- BODY — serious ethnographic tracking, built by ACCRETION OF EVIDENCE, not plot. \
Each beat advances one of: archival and eyewitness accounts and how they were \
recorded; historical geographic distribution (where the entity is attested, its \
spread and borders); regional variation (how the account shifts between communities); \
systemic physical patterns (details that recur across independent accounts — the \
anatomy, the tell, the ritual counter-measure). Open curiosity loops and pay them off.
- DISCARD FICTION ENTIRELY: no invented protagonist, no scene-by-scene story, no \
dramatized victim, no "Little did they know", no spooky-story cliches. You are \
explaining a tradition, not telling a tale.

CULTURAL ACCURACY (non-negotiable — this is our credibility moat):
- Attribute every entity to its TRUE culture of origin, and separate regional \
sub-groups precisely: the Adze is Ewe, NOT Akan; never blur a whole region into one \
people. Name the people, region, and period accurately — getting this wrong destroys \
the channel's authority. Put that culture in "cultural_origin".
- Where the real tradition genuinely varies between groups, say so; do not flatten it.
- Do NOT invent fake scholars, citations, or dates. Stay within what the real \
ethnographic record supports.

VOICE (write for a human narrator, not an AI):
- Authoritative but genuinely curious — a researcher who has read the sources walking \
you through the evidence.
- Vary sentence length hard. Kill the AI tells: no tricolons ("the cold, the dark, \
the silence"), no "not X, but Y", no "stands as a testament", no sentence that just \
restates what the image already shows.
- One concrete, sourced-feeling ethnographic detail per beat, over mood-words. Write \
for the ear; if a line is hard to say aloud, cut it.

MONETIZATION SAFETY (hard rule — this is a YouTube channel):
- Imply, never show. No explicit gore, blood, viscera, wounds, or dismemberment in \
narration or visuals. Convey the unsettling through shadow, silhouette, lighting, \
aftermath, and suggestion. Cut away before the act.
- No graphic harm to children or infants shown or described. Handle any child in the \
lore with heavy restraint or keep them off-screen.
- Scholarly unease, not shock or disgust. No sexual content. Restraint is the \
aesthetic here, not a limitation.

STORYBOARD PLANNING. For each beat give the narration and a matching visual, and \
propose how much motion it deserves. Motion tiers (cost matters — reserve the paid \
one for a handful of hero shots):
- "static": a still + subtle FX (candle flicker, drifting smoke). Cheap.
- "parallax": a 2.5D depth-parallax move on a still. Cheap. Most beats.
- "ai_video": true generated motion. Expensive — only genuine motion beats (a \
transformation, wings unfurling, a face turning). Use sparingly.

For each beat, "style_medium" is a concrete HISTORICAL ART MEDIUM authentic to the \
entity's culture, phrased to lead an image prompt — name the real medium, period, and \
technique. Examples: "a genuine antique ukiyo-e mokuhanga woodblock print, Edo \
period, hand-carved outlines, flat mineral pigment"; "a Slavic lubok woodcut in the \
Ivan Bilibin folk-illustration tradition"; "a medieval illuminated-manuscript codex \
page, egg tempera and gold leaf on vellum"; "a West African bronze relief plaque in \
the Benin court tradition". Usually the SAME medium every beat (one entity, one \
culture); vary it only with good reason. Never put a modern/digital/3D/anime/ \
photographic style here.

"visual" describes ONLY the scene for that beat — subject, composition, strong \
chiaroscuro, deep shadow, cinematic 16:9. Do NOT restate the medium in "visual" (it \
comes from "style_medium"). For s001 the visual is the manuscript cold open itself \
(the hand, the illuminated book, the entity's chapter, the push into the first \
illustration). Favor shadow-play / silhouette for the scariest reveals — cheap, \
always on-model, maximally eerie.

Number beats s001, s002, ... in order, starting with the manuscript cold open."""

# JSON schema the model must fill. Enums keep motion/camera values manifest-valid.
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "cultural_origin": {"type": "string"},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "narration": {"type": "string"},
                    "visual": {"type": "string"},
                    "style_medium": {"type": "string"},
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
                    "style_medium",
                    "suggested_motion_type",
                    "suggested_camera",
                    "suggested_fx",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "cultural_origin", "beats"],
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
                style_medium=beat.get("style_medium", ""),
                motion_type=MotionType(beat.get("suggested_motion_type", "parallax")),
                camera=Camera(move=beat.get("suggested_camera", "push_in")),
                fx=list(beat.get("suggested_fx") or []),
            )
        )
    return Storyboard(
        title=data.get("title", ""),
        cultural_origin=data.get("cultural_origin", ""),
        script_locked=False,
        shots=shots,
    )


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
        scope = "Produce as many beats as the investigation needs (typically 15-40)."

    user_prompt = (
        "Research and script an Illuminated Bestiary documentary episode, in Vesper's "
        "voice, starting with the manuscript cold open and following the Illuminated "
        f"Codex format.\n\nEntity / topic:\n{topic}\n\n{scope}"
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
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary script stage.")
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
