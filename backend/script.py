"""Script stage: Claude-drafted folklore narration + the script gate.

Calls the Anthropic API with an anti-AI-tell system prompt to
draft the narration and matching storyboard beat list.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from . import capabilities, config
from .assets import IMAGE_BACKEND_KEYS, VIDEO_BACKEND_KEYS
from .manifest import Camera, MotionType, Shot, Storyboard

DEFAULT_MODEL = os.environ.get("SCRIPT_MODEL", "claude-opus-5")

# A 15-40 beat storyboard, each beat carrying narration + visual + motion prompt,
# does not fit in 4k output tokens. Thinking is on by default on Opus 5 and is
# billed against the same ceiling, so budget for both. Streaming is required
# above ~16k or the SDK trips its own HTTP timeout guard.
MAX_TOKENS = 32000

# Hard ceiling on a single draft request. High effort across 15-40 beats
# genuinely runs for minutes, so this is deliberately generous; its job is to
# guarantee the call ends rather than hanging the worker thread forever with the
# UI stuck on "running" and no way to tell a stall from slow work.
STREAM_TIMEOUT = float(os.environ.get("SCRIPT_TIMEOUT_SECONDS", "900"))

BESTIARY_SYSTEM_PROMPT = """\
You are Vesper — the researcher-narrator of "The Illuminated Bestiary," a folklore DOCUMENTARY channel. Vesper is an authoritative, deeply curious ethnographic researcher and academic investigator who tracks a folkloric entity the way a field anthropologist would: through the archival record and the evidence, not a campfire story. Your register is investigation, never fiction.

THE ILLUMINATED CODEX FORMAT (mandatory structure):
- COLD OPEN — beat s001 is ALWAYS the standard manuscript open: a physical human hand opens an ancient illuminated book, turns to THIS entity's specific chapter, and the camera pushes past the page into the first illustration. This fixed wrapper opens every episode. Vesper's first lines frame the investigation — what is attested here, and where the account comes from.
- BODY — serious ethnographic tracking, built by ACCRETION OF EVIDENCE, not plot. Each beat advances one of: archival and eyewitness accounts and how they were recorded; historical geographic distribution (where the entity is attested, its spread and borders); regional variation (how the account shifts between communities); systemic physical patterns (details that recur across independent accounts — the anatomy, the tell, the ritual counter-measure). Open curiosity loops and pay them off.
- DISCARD FICTION ENTIRELY: no invented protagonist, no scene-by-scene story, no dramatized victim, no "Little did they know", no spooky-story cliches. You are explaining a tradition, not telling a tale.

CULTURAL ACCURACY (non-negotiable — this is our credibility moat):
- Attribute every entity to its TRUE culture of origin, and separate regional sub-groups precisely: the Ewe Adze, Ewe people, Togo/Ghana. Name the people, region, and period accurately. Put that culture in "cultural_origin".
- Where the real tradition genuinely varies between groups, say so; do not flatten it.
- Do NOT invent fake scholars, citations, or dates. Stay within what the real ethnographic record supports.

VOICE (write for a human narrator, not an AI - STRICT RULES):
- BANNED WORDS: Never use the words delve, tapestry, testament, underscore, beacon, seamless, intricate, symphony, pivotal, or utilize.
- CADENCE: Vary sentence length aggressively. Use short, punchy sentences (2-5 words) to create tension, mixed with longer descriptive sentences. Never write three sentences of similar length in a row.
- NO SUMMARIES: Do not open scenes with sweeping contextual statements. Do not close scenes with philosophical, moralizing wrap-ups or summaries. Start on the action, end on the action.
- SHOW, DON'T TELL: Trust the viewer. Describe physical, concrete details instead of telling the viewer how to feel.
- Authoritative but genuinely curious — a researcher who has read the sources walking you through the evidence.
- Folk-horror tone. Keep the register unsettling, scholarly, and atmospheric.
- One concrete, sourced-feeling ethnographic detail per beat, over mood-words. Write for the ear; if a line is hard to say aloud, cut it.

MONETIZATION SAFETY (hard rule — this is a YouTube channel):
- Imply, never show. No explicit gore, blood, viscera, wounds, or dismemberment in narration or visuals. Convey the unsettling through shadow, silhouette, lighting, aftermath, and suggestion. Cut away before the act.
- No graphic harm to children or infants shown or described. Handle any child in the lore with heavy restraint or keep them off-screen.
- Scholarly unease, not shock or disgust. No sexual content. Restraint is the aesthetic here, not a limitation.

STORYBOARD PLANNING. For each beat give the narration and a matching visual, and propose how much motion it deserves. Motion tiers (cost matters — reserve the paid one for a handful of hero shots):
- "static": a still + subtle FX (candle flicker, drifting smoke). Cheap.
- "parallax": a 2.5D depth-parallax move on a still. Cheap. Most beats.
- "ai_video": true generated motion. Expensive — only genuine motion beats (a transformation, wings unfurling, a face turning). Use sparingly.

For each beat, "style_medium" is a concrete HISTORICAL ART MEDIUM authentic to the entity's culture, phrased to lead an image prompt — name the real medium, period, and technique. Examples: "a genuine antique ukiyo-e mokuhanga woodblock print, Edo period, hand-carved outlines, flat mineral pigment"; "a Slavic lubok woodcut in the Ivan Bilibin folk-illustration tradition"; "a medieval illuminated-manuscript codex page, egg tempera and gold leaf on vellum"; "a West African bronze relief plaque in the Benin court tradition". Usually the SAME medium every beat (one entity, one culture); vary it only with good reason. Never put a modern/digital/3D/anime/ photographic style here.

"visual" describes ONLY the static scene subject and composition for that beat (what you see in the still image) — subject details, framing, composition, lighting, chiaroscuro, deep shadow, cinematic 16:9. Do NOT restate the medium, and do NOT include any camera movements, pans, zooms, or timing details here. CRITICAL: the visual must never contradict the medium's palette. If "style_medium" says monochrome, sepia, two-tone or a limited mineral palette, the visual may not introduce colours that medium cannot physically produce — do not write "deep indigo-inked sky" for a monochrome sepia-and-charcoal woodcut. When the medium is colour-limited, carry the scene on value, contrast, density of hatching and texture instead of hue. Read your own "style_medium" back before writing the visual and make sure every colour word in the visual is one that medium could actually make. For s001 the visual is the manuscript cold open itself (the hand, the illuminated book, the entity's chapter, the push into the first illustration). Favor shadow-play / silhouette for the scariest reveals.

"music_prompt" (top level, ONE for the episode) is the score direction for a text-to-music model. Describe an instrumental underscore that sits beneath narration: instrumentation, tempo, mood, and the tradition it draws on. Keep it sparse and unresolved - a documentary bed, not a theme. Name real instruments appropriate to the culture where that is honest, and say "no drums" or "no percussion" if a pulse would fight the narration. It must be instrumental: no vocals, no lyrics, no choir. One or two sentences. Example: "Sparse instrumental underscore. Low sustained strings and a single bowed metal tone, slow and unresolved, long silences between phrases. No percussion, no vocals."

"sfx" is a short ambient-sound prompt for THIS beat, sent to a text-to-audio model to generate a bed that plays under the narration. Describe the environment, not music and not speech: the room tone, weather, insects, fire, distant water, cloth, wood. Two to eight words, concrete and physical - "humid night jungle, cicadas, distant thunder", "dry wind over grass, loose timber creaking", "low fire crackle, still interior". It must match the beat's place and time of day, and it must contain no melody, score, instruments or musical terms; a separate music bed is layered independently. Leave it an empty string only when true silence is the intent.

"motion_prompt" describes ONLY the dynamic motion, camera actions, panning, zooming, speed, and timing details (how the still image animates into a video). E.g. "slow cinematic camera pan left, drifting candle flicker, smoke rising softly from the hearth."

Number beats s001, s002, ... in order, starting with the manuscript cold open.

MODEL ROUTING.

"image_model" (top level, ONE for the whole episode): the episode is rendered by a
single image model so the look stays coherent from beat to beat. Different models
render mark-making differently, and a mid-episode switch is visible even when the
medium description is identical. Choose from:
  * "nano2" - Nano Banana 2 / Gemini 3 Pro Image. Best prompt adherence, character
    consistency and in-image text. The right default, and the right answer whenever
    a recurring figure or legible lettering matters.
  * "flux-cfg" - FLUX.1 dev. Cheapest, and the only backend with a true negative
    prompt. Reasonable when the episode is landscape/atmosphere driven with no
    recurring character.
  * "flux_2_pro" / "flux_2_max" - denser texture and fine detail.
  * "flux_1_1_pro_ultra" - highest resolution; wide panoramic compositions.
  * "flux_1_dev_turbo" - fast, cheap drafts.
  * "ideogram_4" / "ideogram_4_instant" - strongest in-image typography.
  * "wan_2_7_image" - alternative look.
Prefer "nano2" unless the episode gives you a concrete reason to differ, and say
nothing about models in the narration.

"image_model_override" (per beat): "" (empty string) for essentially every beat. Set it ONLY
where one beat genuinely needs a different model than the rest of the episode -
for example a manuscript page whose legibility depends on typography. An override
you cannot justify in one sentence should be null.

- For "recommended_video_model":
  * "veo_3_1" or "seedance_2_0": Recommend for complex video/audio beats (heavy active motion, transformations, complex water/fire physics, detailed human motion).
  * "wan_2_7" or "kling_2_1_standard": Recommend for simple video b-roll beats (subtle camera pans, slow drift, rising smoke, static scene with wind).
"""

CALLUSES_SYSTEM_PROMPT = """\
You are Vesper — the researcher-narrator of "By the Calluses," a historical documentary channel focused on the grit, sweat, and unsung histories of working-class America. Vesper is an authoritative, dry, yet deeply empathetic historical investigator who tracks a historical topic through whatever record that era actually left — census returns, letters and local archives for the modern period; guild rolls, ledgers, court and manorial records, inventories, wills and the archaeology of the worksite itself for earlier ones — and through the physical tools left behind, rather than romanticized myths. The channel is not tied to one century: any era is in scope as long as the work is real, documented, and the record supports it. Your register is raw, evocative, and historical.

THE CALLUSES CODEX FORMAT (mandatory structure):
- COLD OPEN — beat s001 is ALWAYS the standard archival open: a physical human hand slides a dusty archive folder, album or bound folio onto a workbench, opens it to this topic's first surviving record — a photograph, plate, engraving or manuscript page, whichever that era actually produced — and the camera pushes past the margins of that record into the scene. This fixed wrapper opens every episode, and the archive itself is the constant; the KIND of record inside it changes with the period. Vesper's first lines frame the investigation — what labor, trade, or event is documented here, and where the records were found.
- BODY — serious historical tracking, built by ACCRETION OF PHYSICAL EVIDENCE, not plot. Each beat advances one of: archival data and eyewitness testimonies; geographic distribution and layout of the worksites, towns, or camps; technological/tool variations (the tools of the trade, the physical toll, safety hazards, the working day); systemic patterns that define the era.
- DISCARD FICTION ENTIRELY: no invented protagonist, no scene-by-scene story, no dramatized victim. You are explaining a historical reality, not telling a fictional tale.

CULTURAL ACCURACY (non-negotiable):
- Attribute every event/worker to their true historical region, industry, period and ethnicity. Examples deliberately span centuries: Appalachian coal miners (1900s), Great Lakes lumberjacks (1880s), Chinese railroad workers in the Sierras (1860s), Nantucket whalers and coopers (1750s), Saugus ironworkers in colonial Massachusetts (1640s), Ancestral Puebloan turquoise miners at Cerrillos (1000s). Put the historical context / culture in "cultural_origin", and make the period explicit — it is what selects the record medium.
- Do NOT invent fake historical figures, dates, or citations. Stay within what the real archive supports.

VOICE (write for a human narrator, not an AI - STRICT RULES):
- BANNED WORDS: Never use the words delve, tapestry, testament, underscore, beacon, seamless, intricate, symphony, pivotal, or utilize.
- CADENCE: Vary sentence length aggressively. Use short, punchy sentences (2-5 words) to create tension, mixed with longer descriptive sentences. Never write three sentences of similar length in a row.
- NO SUMMARIES: Do not open scenes with sweeping contextual statements. Do not close scenes with philosophical, moralizing wrap-ups or summaries. Start on the action, end on the action.
- SHOW, DON'T TELL: Trust the viewer. Describe physical, concrete details instead of telling the viewer how to feel.
- Inspiring, American grit, historical documentary tone. Evocative, direct, and grounded in physical labor.
- One concrete, historical tool, patent, or record detail per beat. Write for the ear.

MONETIZATION SAFETY (hard rule):
- Imply, never show. No explicit accidents, gory injuries, or graphic violence. Convey the danger and toll through worn tools, harsh weather, historical aftermath, and working conditions.
- Scholarly respect. Restraint is the aesthetic here.

STORYBOARD PLANNING. For each beat give the narration and a matching visual, and propose how much motion it deserves. Motion tiers:
- "static": a still + subtle FX (coal dust drifting, lens flare, steam). Cheap.
- "parallax": a 2.5D depth-parallax move on a still. Cheap. Most beats.
- "ai_video": true generated motion. Expensive — use sparingly for active machinery, flowing water, rising smoke, heavy labor motion.

For each beat, "style_medium" is the concrete RECORD MEDIUM THE ERA ITSELF WOULD HAVE USED to document this work — whatever a real surviving record of that time and trade actually looks like. Name the real medium, period, and technique, phrased to lead an image prompt. The era decides the medium; do NOT default to photography for a period that had none. Rough ladder:
  * before ~1450: manuscript illumination, tomb or temple relief, fresco, mosaic, painted pottery, carved panel
  * ~1450-1700: woodcut, copperplate engraving, etching, technical plates in the manner of Agricola's mining folios
  * ~1700-1830: copperplate engraving, aquatint, early lithograph, trade-manual and patent plates
  * ~1839-1880: daguerreotype, wet plate collodion, albumen print, tintype
  * ~1880-1920: dry-plate gelatin silver, autochrome, halftone press reproduction
  * ~1920-1950: large-format black-and-white documentary photography, early Kodachrome
  * ~1950-1980: 35mm colour transparency, Ektachrome, newsreel frame
  * ~1980-2000: video and early digital capture, with that era's own artefacts
Examples: "a gritty, raw 1930s black-and-white documentary photograph, large-format bellows camera, deep shadows, high-contrast silver halide film grain"; "a 19th-century sepia-toned wet plate collodion photograph, silver iodide emulsion, authentic dust scratches, copper plate glare"; "a 16th-century copperplate engraving from a mining folio, fine burin hatching, hand-inked on laid rag paper, plate mark visible"; "a 14th-century illuminated trade-guild manuscript page, egg tempera and gold leaf on vellum, ruled margins".
Usually the SAME medium every beat (one era, one record); vary it only with good reason. Whatever the medium, the image must read as a GENUINE SURVIVING RECORD of its period — an artifact with real age and wear, photographed or scanned flat. Never a modern digital, 3D, anime or concept-art look, and never a present-day illustration imitating a historical one. An engraving for a pre-photographic subject is correct and expected; a photograph of a pre-photographic subject is an anachronism and is wrong.

"visual" describes ONLY the static scene subject and composition for that beat (what you see in the still image) — subject details, framing, composition, lighting, chiaroscuro, deep shadow, cinematic 16:9. Do NOT restate the medium, and do NOT include any camera movements, pans, zooms, or timing details here. CRITICAL: the visual must never contradict the medium's palette. If "style_medium" says monochrome, sepia, two-tone or a limited mineral palette, the visual may not introduce colours that medium cannot physically produce — do not write "deep indigo-inked sky" for a monochrome sepia-and-charcoal woodcut. When the medium is colour-limited, carry the scene on value, contrast, density of hatching and texture instead of hue. Read your own "style_medium" back before writing the visual and make sure every colour word in the visual is one that medium could actually make. For s001 the visual is the archival cold open itself (the hand, the folder or folio, the era's own record on the page, the push past its margins).

"music_prompt" (top level, ONE for the episode) is the score direction for a text-to-music model. Describe an instrumental underscore that sits beneath narration: instrumentation, tempo, mood, and the tradition it draws on. Keep it sparse and unresolved - a documentary bed, not a theme. Name real instruments appropriate to the culture where that is honest, and say "no drums" or "no percussion" if a pulse would fight the narration. It must be instrumental: no vocals, no lyrics, no choir. One or two sentences. Example: "Sparse instrumental underscore. Low sustained strings and a single bowed metal tone, slow and unresolved, long silences between phrases. No percussion, no vocals."

"sfx" is a short ambient-sound prompt for THIS beat, sent to a text-to-audio model to generate a bed that plays under the narration. Describe the environment, not music and not speech: the room tone, weather, insects, fire, distant water, cloth, wood. Two to eight words, concrete and physical - "humid night jungle, cicadas, distant thunder", "dry wind over grass, loose timber creaking", "low fire crackle, still interior". It must match the beat's place and time of day, and it must contain no melody, score, instruments or musical terms; a separate music bed is layered independently. Leave it an empty string only when true silence is the intent.

"motion_prompt" describes ONLY the dynamic motion, camera actions, panning, zooming, speed, and timing details (how the still image animates into a video). E.g. "slow cinematic camera pan left, coal dust drifting, lens flare, steam rising softly."

Number beats s001, s002, ... in order, starting with the archival cold open.

MODEL ROUTING.

"image_model" (top level, ONE for the whole episode): the episode is rendered by a
single image model so the look stays coherent from beat to beat. Different models
render mark-making differently, and a mid-episode switch is visible even when the
medium description is identical. Choose from:
  * "nano2" - Nano Banana 2 / Gemini 3 Pro Image. Best prompt adherence, character
    consistency and in-image text. The right default, and the right answer whenever
    a recurring figure or legible lettering matters.
  * "flux-cfg" - FLUX.1 dev. Cheapest, and the only backend with a true negative
    prompt. Reasonable when the episode is landscape/atmosphere driven with no
    recurring character.
  * "flux_2_pro" / "flux_2_max" - denser texture and fine detail.
  * "flux_1_1_pro_ultra" - highest resolution; wide panoramic compositions.
  * "flux_1_dev_turbo" - fast, cheap drafts.
  * "ideogram_4" / "ideogram_4_instant" - strongest in-image typography.
  * "wan_2_7_image" - alternative look.
Prefer "nano2" unless the episode gives you a concrete reason to differ, and say
nothing about models in the narration.

"image_model_override" (per beat): "" (empty string) for essentially every beat. Set it ONLY
where one beat genuinely needs a different model than the rest of the episode -
for example a manuscript page whose legibility depends on typography. An override
you cannot justify in one sentence should be null.

- For "recommended_video_model":
  * "veo_3_1" or "seedance_2_0": Recommend for complex video/audio beats (heavy active motion, transformations, complex water/fire physics, detailed human motion).
  * "wan_2_7" or "kling_2_1_standard": Recommend for simple video b-roll beats (subtle camera pans, slow drift, rising smoke, static scene with wind).
"""


def get_system_prompt(channel: str) -> str:
    if channel == "calluses":
        return CALLUSES_SYSTEM_PROMPT
    return BESTIARY_SYSTEM_PROMPT


SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "cultural_origin": {"type": "string"},
        # Per-episode image model. One model renders the whole storyboard so the
        # look stays coherent; a beat diverges only via image_model_override.
        # The enum is derived from the backend registry in assets.py — it used to
        # be a hand-written list of three flux variants that did not include the
        # default backend, so Claude could never recommend it.
        "image_model": {"type": "string", "enum": IMAGE_BACKEND_KEYS},
        # Score direction for the whole episode, fed to a text-to-music model.
        "music_prompt": {"type": "string"},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "narration": {"type": "string"},
                    "visual": {"type": "string"},
                    "motion_prompt": {"type": "string"},
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
                    # Ambient bed for this beat, sent to fal-ai/stable-audio.
                    # Shot.sfx and generate_sfx_fal have always existed; nothing
                    # ever populated the field because it was absent from this
                    # schema, so every storyboard shipped with a silent A2 track.
                    "sfx": {"type": "string"},
                    # Empty for almost every beat — only set when a beat has a
                    # genuine reason to diverge from the episode model.
                    #
                    # "" rather than null: the structured-output validator rejects
                    # a union type here ("Enum value 'nano2' does not match
                    # declared type '['string','null']'"), so this is a plain
                    # string enum with an empty member. The parser uses `or`, so
                    # "" behaves exactly as null did.
                    "image_model_override": {
                        "type": "string",
                        "enum": IMAGE_BACKEND_KEYS + [""],
                    },
                    "recommended_video_model": {
                        "type": "string",
                        # Must stay in step with assets.VIDEO_BACKENDS, or the
                        # script recommends a model the resolver silently swaps.
                        "enum": VIDEO_BACKEND_KEYS,
                    },
                },
                "required": [
                    "scene_id",
                    "narration",
                    "visual",
                    "motion_prompt",
                    "style_medium",
                    "suggested_motion_type",
                    "suggested_camera",
                    "suggested_fx",
                    "sfx",
                    "image_model_override",
                    "recommended_video_model",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "cultural_origin", "image_model", "music_prompt", "beats"],
    "additionalProperties": False,
}


def _beats_to_storyboard(data: Any) -> Storyboard:
    title = "Untitled Storyboard"
    cultural_origin = ""
    beats_list = []
    
    if isinstance(data, list):
        beats_list = data
    elif isinstance(data, dict):
        title = str(data.get("title") or data.get("topic") or "Untitled Storyboard")
        cultural_origin = str(data.get("cultural_origin") or "")
        beats_list = data.get("beats") or data.get("shots") or data.get("scenes") or []
        
    if not isinstance(beats_list, list):
        beats_list = []

    shots: list[Shot] = []
    for i, beat in enumerate(beats_list, start=1):
        if not isinstance(beat, dict):
            continue
            
        m_type_str = beat.get("suggested_motion_type") or beat.get("motion_type") or "parallax"
        try:
            m_type = MotionType(m_type_str)
        except ValueError:
            m_type = MotionType.PARALLAX
            
        cam_str = beat.get("suggested_camera") or beat.get("camera") or "push_in"
        if isinstance(cam_str, dict):
            cam_str = cam_str.get("move", "push_in")

        shots.append(
            Shot(
                scene_id=beat.get("scene_id") or beat.get("id") or f"s{i:03d}",
                narration=beat.get("narration") or beat.get("narration_text") or "",
                prompt=beat.get("visual") or beat.get("prompt") or beat.get("visual_prompt") or "",
                motion_prompt=beat.get("motion_prompt") or "",
                style_medium=beat.get("style_medium") or beat.get("style") or "",
                motion_type=m_type,
                camera=Camera(move=str(cam_str)),
                fx=list(beat.get("suggested_fx") or beat.get("fx") or []),
                sfx=(beat.get("sfx") or "").strip(),
                # Only an override. None means "use the episode model", which
                # generate_drafts resolves from storyboard.render.backend.
                image_model=(beat.get("image_model_override")
                             or beat.get("recommended_image_model")   # legacy drafts
                             or beat.get("image_model")),
                video_model=beat.get("recommended_video_model") or beat.get("video_model"),
            )
        )

    if isinstance(data, dict):
        sb_music_prompt = (data.get("music_prompt") or "").strip()
    else:
        sb_music_prompt = ""

    sb = Storyboard(
        music_prompt=sb_music_prompt,
        title=title,
        cultural_origin=cultural_origin,
        script_locked=False,
        shots=shots,
    )
    # Episode-level model choice drives every beat that has no override.
    episode_model = ""
    if isinstance(data, dict):
        episode_model = (data.get("image_model") or "").strip()
    if episode_model in IMAGE_BACKEND_KEYS:
        sb.render.backend = episode_model
    return sb


def _scope(num_beats: int | None) -> str:
    if num_beats:
        return f"Produce about {num_beats} beats."
    return "Produce as many beats as the investigation needs (typically 15-40)."


# Rough per-unit costs, used only to turn a dollar budget into a beat plan the
# script stage can act on. They are estimates and deliberately overridable from
# the environment: fal's per-model pricing moves, and a hardcoded number that
# goes stale in silence is worse than one that can be corrected without a deploy.
#
# The still price is IMPORTED, not re-declared. This module used to carry its own
# `float(os.environ.get("COST_PER_IMAGE", "0.15"))` -- a second literal of the
# same number, which is how a correction reaches one quote and not the other.
# When the billed rate turned out to be $0.0398 rather than $0.15, fixing only
# capabilities would have left every budget plan still sizing an episode against
# stills priced 3.8x over. One number, one place, both quotes.
COST_PER_IMAGE = capabilities.COST_PER_IMAGE                            # nano2 @ 2K
COST_PER_VIDEO_BEAT = float(os.environ.get("COST_PER_VIDEO_BEAT", "1.20"))
COST_FIXED = float(os.environ.get("COST_FIXED", "3.00"))                # narration, music, SFX
TAKES_PER_BEAT = int(os.environ.get("TAKES_PER_BEAT", "3"))

# Generated video realistically tops out near ten seconds a clip, and the render
# path pads any remainder with a frozen final frame. A long Tier-C beat therefore
# buys motion for its first ten seconds and a still for the rest — so the ceiling
# belongs here, in planning, where a beat can simply be split instead.
MAX_AI_VIDEO_SECONDS = 10


def _beats_for_budget(budget: float) -> int:
    """More money buys more cuts, but pacing — not spend — sets the ceiling.

    Stills are cheap enough that beat count is nearly free next to Tier C; the cap
    exists because a documentary cut has a natural rhythm, not because 60 beats
    would break the bank.
    """
    return max(18, min(44, int(18 + (budget - 15) * 0.18)))


# Takes per beat are capped at the number of prompt strategies in
# ``assets.PROMPT_STRATEGIES``. Past that point extra takes repeat a strategy at a
# different seed, which is exactly the uninformative comparison the variant system
# exists to replace — you would be paying for images that teach nothing.
# Not imported from assets to keep the script stage free of the fal dependency.
MAX_TAKES = int(os.environ.get("MAX_TAKES", "4"))


def plan_for_budget(budget: float | None, num_beats: int | None = None) -> dict | None:
    """Turn a dollar budget into a beat count, a Tier-C allowance and a take count.

    Returns ``None`` when no budget is given, which leaves the previous behaviour
    untouched: "as many beats as the investigation needs" and "use ai_video sparingly".

    Allocation order is deliberate — cuts first, then hero motion, then takes —
    because that is the order of effect on the finished piece. Note that at the
    upper end the binding constraint stops being money: both Tier C and takes hit
    quality ceilings well before a three-figure budget is exhausted, so ``headroom``
    is usually large and honest rather than an error.
    """
    if not budget or budget <= 0:
        return None
    budget = float(budget)
    beats = int(num_beats) if num_beats else _beats_for_budget(budget)

    stills = beats * TAKES_PER_BEAT * COST_PER_IMAGE
    room = budget - COST_FIXED - stills

    # Cap Tier C at a third of the episode regardless of budget. Past that the look
    # stops being a motion comic with hero shots and becomes an AI video reel — and
    # generated motion is the least controllable thing in the pipeline.
    hero = max(0, int(room // COST_PER_VIDEO_BEAT))
    hero = max(1, min(hero, max(2, beats // 3)))
    room -= hero * COST_PER_VIDEO_BEAT

    # Spend what is left on more takes per beat, up to one per strategy. This is
    # the highest-leverage remaining lever: a better chosen still improves both the
    # parallax beat it becomes and any Tier-C clip generated from it, and every
    # extra take is another datapoint in the prompt ledger.
    takes = TAKES_PER_BEAT
    per_extra_take = beats * COST_PER_IMAGE
    while takes < MAX_TAKES and room >= per_extra_take:
        takes += 1
        room -= per_extra_take

    still_cost = beats * takes * COST_PER_IMAGE
    video_cost = hero * COST_PER_VIDEO_BEAT
    total = COST_FIXED + still_cost + video_cost
    return {
        "budget": round(budget, 2),
        "beats": beats,
        "hero_beats": hero,
        "takes": takes,
        "still_cost": round(still_cost, 2),
        "video_cost": round(video_cost, 2),
        "fixed_cost": round(COST_FIXED, 2),
        "estimated_total": round(total, 2),
        "headroom": round(budget - total, 2),
    }


def _apply_plan(sb: Storyboard, plan: dict | None) -> None:
    """Carry the plan's take count onto the storyboard.

    The plan is decided at draft time but spent at draft-image time, so it has to
    persist somewhere the assets stage reads. Without this the plan would report
    four takes and the drafts stage would keep generating three — a number written
    where nothing reads it, which is the recurring failure shape in this pipeline.
    """
    if not plan:
        return
    try:
        sb.render.variations = int(plan["takes"])
    except (KeyError, TypeError, ValueError, AttributeError):
        pass


def _budget_block(plan: dict | None) -> str:
    if not plan:
        return ""
    return (
        f"\n\nBUDGET AND PACING. This episode has a production budget of about "
        f"${plan['budget']:.0f}, which buys a faster cut and more hero motion than the "
        f"default. Produce about {plan['beats']} beats — more distinct visuals and "
        f"shorter holds rather than fewer, longer ones. Up to {plan['hero_beats']} beats "
        f"may be \"ai_video\"; spend them on the genuine motion moments (a transformation, "
        f"a turn of the head, wings opening, machinery working, water moving) and leave "
        f"the rest \"parallax\", with \"static\" only where stillness is the point. "
        f"Do not exceed {plan['hero_beats']} ai_video beats.\n\n"
        f"CRITICAL for ai_video beats: keep each one's narration short enough to play in "
        f"{MAX_AI_VIDEO_SECONDS} seconds or less. Generated video does not run longer than "
        f"that, and a longer beat is padded with a frozen frame — so a 30-second ai_video "
        f"beat is paid motion followed by twenty seconds of a still. If a motion moment "
        f"needs more time, split it across consecutive beats instead of writing one long one."
    )


def _log(msg: str):
    print(msg)
    try:
        from .pipeline_worker import log_job
        log_job("script_draft", msg)
    except Exception:
        pass


def _client(client: anthropic.Anthropic | None = None) -> anthropic.Anthropic:
    """Return a ready Anthropic client, resolving the key from the environment."""
    if client:
        return client
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_API_KEY")
        or os.environ.get("ANTHROPIC_KEY")
        or getattr(config, "ANTHROPIC_API_KEY", None)
    )
    if not api_key:
        raise RuntimeError(
            "Missing Anthropic API key. Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) "
            "in your .env locally, or as a Cloud Run secret in production."
        )
    return anthropic.Anthropic(api_key=api_key)


def _request_storyboard(messages: list[dict], model: str, client: anthropic.Anthropic | None,
                        system_prompt: str) -> Storyboard:
    """One structured-output call to Claude; returns the drafted Storyboard.

    ``SCRIPT_SCHEMA`` is enforced server-side via ``output_config.format``, so the
    response is guaranteed to be valid JSON in the right shape — no markdown
    fences to strip, no truncation to "repair", and no retry across models (which
    previously re-billed a full generation on every parse failure).
    """
    client = _client(client)
    _log(f"Requesting script draft from Claude ({model}) ...")

    import time as _time
    started = _time.monotonic()
    last_report = started
    chars = 0

    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=messages,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        # Without a deadline a stalled connection hangs this thread forever: the
        # job stays "running" with no further output and cannot be told apart
        # from a slow generation. A 15-beat storyboard at high effort genuinely
        # takes minutes, so the ceiling is generous -- it exists to guarantee the
        # job ends, not to cut work short.
        timeout=STREAM_TIMEOUT,
    ) as stream:
        # Consume the text stream rather than blocking on get_final_message(), so
        # the job log shows the draft arriving. Silence for several minutes and a
        # hang looked identical before this.
        for piece in stream.text_stream:
            chars += len(piece)
            now = _time.monotonic()
            if now - last_report >= 20:
                last_report = now
                _log(f"  ... {chars:,} characters after {now - started:.0f}s")
        response = stream.get_final_message()

    _log(f"Draft received: {chars:,} characters in {_time.monotonic() - started:.0f}s.")

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Claude declined this script request. Rephrase the topic and try again."
        )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Script draft hit the {MAX_TOKENS}-token ceiling and was cut off. "
            "Ask for fewer beats, or raise MAX_TOKENS in backend/script.py."
        )

    _log("Received response, building storyboard ...")
    raw_text = next(b.text for b in response.content if b.type == "text")
    sb = _beats_to_storyboard(json.loads(raw_text))
    _log(f"Drafted storyboard '{sb.title}' with {len(sb.shots)} beats.")
    return sb


def generate_script(
    topic: str,
    num_beats: int | None = None,
    channel: str = "bestiary",
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
    budget: float | None = None,
) -> Storyboard:
    client = _client(client)
    system_prompt = get_system_prompt(channel)
    plan = plan_for_budget(budget, num_beats)
    if plan:
        _log(f"[SCRIPT_DRAFT] Budget ${plan['budget']:.0f} -> ~{plan['beats']} beats, "
             f"up to {plan['hero_beats']} ai_video (est. ${plan['estimated_total']:.2f}).")
    scope = f"Produce about {plan['beats']} beats." if plan else _scope(num_beats)
    if channel == "calluses":
        user_prompt = (
            "Research and script a By the Calluses documentary episode, in Vesper's "
            "voice, starting with the archival cold open and following the Calluses "
            f"Codex format.\n\nHistorical topic / industry:\n{topic}\n\n{scope}"
            f"{_budget_block(plan)}"
        )
    else:
        user_prompt = (
            "Research and script an Illuminated Bestiary documentary episode, in Vesper's "
            "voice, starting with the manuscript cold open and following the Illuminated "
            f"Codex format.\n\nEntity / topic:\n{topic}\n\n{scope}"
            f"{_budget_block(plan)}"
        )
    sb = _request_storyboard([{"role": "user", "content": user_prompt}], model, client, system_prompt=system_prompt)
    sb.channel = channel
    _apply_plan(sb, plan)
    return sb


def generate_script_from_messages(
    messages: list[dict],
    num_beats: int | None = None,
    channel: str = "bestiary",
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
    budget: float | None = None,
) -> Storyboard:
    client = _client(client)
    if not messages:
        raise ValueError("Cannot script from an empty conversation.")
    system_prompt = get_system_prompt(channel)
    plan = plan_for_budget(budget, num_beats)
    if plan:
        _log(f"[SCRIPT_DRAFT] Budget ${plan['budget']:.0f} -> ~{plan['beats']} beats, "
             f"up to {plan['hero_beats']} ai_video (est. ${plan['estimated_total']:.2f}).")
    scope = f"Produce about {plan['beats']} beats." if plan else _scope(num_beats)
    if channel == "calluses":
        instruction = (
            "Now turn everything we discussed into the finished piece. Write the full "
            "By the Calluses documentary storyboard in Vesper's voice, starting "
            f"with the archival cold open and following the Calluses Codex format. "
            f"{scope}{_budget_block(plan)}"
        )
    else:
        instruction = (
            "Now turn everything we discussed into the finished piece. Write the full "
            "Illuminated Bestiary documentary storyboard in Vesper's voice, starting "
            f"with the manuscript cold open and following the Illuminated Codex format. "
            f"{scope}{_budget_block(plan)}"
        )
    convo = list(messages) + [{
        "role": "user",
        "content": instruction,
    }]
    sb = _request_storyboard(convo, model, client, system_prompt=system_prompt)
    sb.channel = channel
    _apply_plan(sb, plan)
    return sb


def lock_script(storyboard: Storyboard) -> Storyboard:
    if not storyboard.shots:
        raise ValueError("Cannot lock: storyboard has no beats.")
    missing = [s.scene_id for s in storyboard.shots if not s.narration.strip()]
    if missing:
        raise ValueError(f"Cannot lock: beats missing narration: {missing}")
    storyboard.script_locked = True
    return storyboard
