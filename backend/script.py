"""Script stage: Claude-drafted folklore narration + the script gate.

Calls the Anthropic API with an anti-AI-tell system prompt to
draft the narration and matching storyboard beat list.
"""

from __future__ import annotations

import json
import os
import anthropic

from .manifest import Camera, MotionType, Shot, Storyboard

DEFAULT_MODEL = os.environ.get("SCRIPT_MODEL", "claude-3-5-sonnet-20241022")

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

"visual" describes ONLY the static scene subject and composition for that beat (what you see in the still image) — subject details, framing, composition, lighting, chiaroscuro, deep shadow, cinematic 16:9. Do NOT restate the medium, and do NOT include any camera movements, pans, zooms, or timing details here. For s001 the visual is the manuscript cold open itself (the hand, the illuminated book, the entity's chapter, the push into the first illustration). Favor shadow-play / silhouette for the scariest reveals.

"motion_prompt" describes ONLY the dynamic motion, camera actions, panning, zooming, speed, and timing details (how the still image animates into a video). E.g. "slow cinematic camera pan left, drifting candle flicker, smoke rising softly from the hearth."

Number beats s001, s002, ... in order, starting with the manuscript cold open.

AI MODEL ROUTING GUIDELINES (mandatory model assignment for each beat):
You must assign a specific image and video model to each beat based on its complexity:
- For "recommended_image_model":
  * "flux_2_pro": Recommend for complex, cinematic reference images (highly detailed scenes, rich textures, multiple characters/historical artifacts) optimized for cost and quality.
  * "flux_1_1_pro_ultra": Recommend for complex, cinematic reference images when wide panning shots are required.
  * "flux_1_dev_turbo": Recommend for fast image drafts.
- For "recommended_video_model":
  * "veo_3_1" or "seedance_2_0": Recommend for complex video/audio beats (heavy active motion, transformations, complex water/fire physics, detailed human motion).
  * "wan_2_7" or "kling_2_5_turbo_pro": Recommend for simple video b-roll beats (subtle camera pans, slow drift, rising smoke, static scene with wind).
"""

CALLUSES_SYSTEM_PROMPT = """\
You are Vesper — the researcher-narrator of "By the Calluses," a historical documentary channel focused on the grit, sweat, and unsung histories of working-class America. Vesper is an authoritative, dry, yet deeply empathetic historical investigator who tracks a historical topic through the census records, letters, local archives, and physical tools left behind, rather than romanticized myths. Your register is raw, evocative, and historical.

THE CALLUSES CODEX FORMAT (mandatory structure):
- COLD OPEN — beat s001 is ALWAYS the standard archival open: a physical human hand slides a dusty historical archive folder or album onto a workbench, opens it to this topic's first vintage photograph or document, and the camera pushes past the margins of the photograph into the scene. This fixed wrapper opens every episode. Vesper's first lines frame the investigation — what labor, trade, or event is documented here, and where the records were found.
- BODY — serious historical tracking, built by ACCRETION OF PHYSICAL EVIDENCE, not plot. Each beat advances one of: archival data and eyewitness testimonies; geographic distribution and layout of the worksites, towns, or camps; technological/tool variations (the tools of the trade, the physical toll, safety hazards, the working day); systemic patterns that define the era.
- DISCARD FICTION ENTIRELY: no invented protagonist, no scene-by-scene story, no dramatized victim. You are explaining a historical reality, not telling a fictional tale.

CULTURAL ACCURACY (non-negotiable):
- Attribute every event/worker to their true historical region, industry, and ethnicity (e.g. Appalachian coal miners, Great Lakes lumberjacks, Chinese railroad workers in the Sierras). Put the historical context / culture in "cultural_origin".
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

For each beat, "style_medium" is a concrete VINTAGE PHOTOGRAPHY/DOCUMENT MEDIUM authentic to the era, phrased to lead an image prompt — name the real medium, period, camera, and photographic technique. Examples: "a gritty, raw 1930s black-and-white documentary photograph, large-format bellows camera, deep shadows, high-contrast silver halide film grain, authentic historical detail"; "a 19th-century sepia-toned wet plate collodion photograph, silver iodide emulsion, authentic dust scratches, copper plate glare"; "a vintage 1910s autochrome color photograph, coarse starch-grain texture, soft atmospheric lighting, authentic historical labor setting". Usually the SAME medium every beat; vary it only with good reason. Never put a modern/digital/3D/anime/illustration style here.

"visual" describes ONLY the static scene subject and composition for that beat (what you see in the still image) — subject details, framing, composition, lighting, chiaroscuro, deep shadow, cinematic 16:9. Do NOT restate the medium, and do NOT include any camera movements, pans, zooms, or timing details here. For s001 the visual is the archival cold open itself (the hand, the folder, the vintage photo, the push past margins).

"motion_prompt" describes ONLY the dynamic motion, camera actions, panning, zooming, speed, and timing details (how the still image animates into a video). E.g. "slow cinematic camera pan left, coal dust drifting, lens flare, steam rising softly."

Number beats s001, s002, ... in order, starting with the archival cold open.

AI MODEL ROUTING GUIDELINES (mandatory model assignment for each beat):
You must assign a specific image and video model to each beat based on its complexity:
- For "recommended_image_model":
  * "flux_2_pro": Recommend for complex, cinematic reference images (highly detailed scenes, rich textures, multiple characters/historical artifacts) optimized for cost and quality.
  * "flux_1_1_pro_ultra": Recommend for complex, cinematic reference images when wide panning shots are required.
  * "flux_1_dev_turbo": Recommend for fast image drafts.
- For "recommended_video_model":
  * "veo_3_1" or "seedance_2_0": Recommend for complex video/audio beats (heavy active motion, transformations, complex water/fire physics, detailed human motion).
  * "wan_2_7" or "kling_2_5_turbo_pro": Recommend for simple video b-roll beats (subtle camera pans, slow drift, rising smoke, static scene with wind).
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
                    "recommended_image_model": {
                        "type": "string",
                        "enum": ["flux_2_pro", "flux_1_1_pro_ultra", "flux_1_dev_turbo"],
                    },
                    "recommended_video_model": {
                        "type": "string",
                        "enum": ["veo_3_1", "seedance_2_0", "wan_2_7", "kling_2_5_turbo_pro"],
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
                    "recommended_image_model",
                    "recommended_video_model",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "cultural_origin", "beats"],
    "additionalProperties": False,
}


def _beats_to_storyboard(data: dict) -> Storyboard:
    shots: list[Shot] = []
    for i, beat in enumerate(data.get("beats", []), start=1):
        shots.append(
            Shot(
                scene_id=beat.get("scene_id") or f"s{i:03d}",
                narration=beat.get("narration", ""),
                prompt=beat.get("visual", ""),
                motion_prompt=beat.get("motion_prompt", ""),
                style_medium=beat.get("style_medium", ""),
                motion_type=MotionType(beat.get("suggested_motion_type", "parallax")),
                camera=Camera(move=beat.get("suggested_camera", "push_in")),
                fx=list(beat.get("suggested_fx") or []),
                image_model=beat.get("recommended_image_model"),
                video_model=beat.get("recommended_video_model"),
            )
        )
    return Storyboard(
        title=data.get("title", ""),
        cultural_origin=data.get("cultural_origin", ""),
        script_locked=False,
        shots=shots,
    )


def _scope(num_beats: int | None) -> str:
    if num_beats:
        return f"Produce about {num_beats} beats."
    return "Produce as many beats as the investigation needs (typically 15-40)."


def normalize_claude_model(model_name: str) -> str:
    """Map legacy/alias Claude model names to valid date-versioned Anthropic model IDs."""
    if not model_name:
        return "claude-3-5-sonnet-20241022"
    m = str(model_name).strip().lower()
    alias_map = {
        "claude-3-5-sonnet-latest": "claude-3-5-sonnet-20241022",
        "claude-3-opus-latest": "claude-3-opus-20240229",
        "claude-3-5-haiku-latest": "claude-3-5-haiku-20241022",
        "claude-3-haiku-latest": "claude-3-haiku-20240307",
        "claude-sonnet-5": "claude-3-5-sonnet-20241022",
        "claude-opus-4-8": "claude-3-opus-20240229",
        "claude-fable-5": "claude-3-5-sonnet-20241022",
        "claude-sonnet-4-6": "claude-3-5-sonnet-20241022",
    }
    return alias_map.get(m, m)


def _parse_json_robust(raw_text: str) -> dict:
    """Parse JSON with auto-repair for trailing truncation if needed."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        import re
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1).strip()
            
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        # Attempt simple auto-closure for truncated JSON objects
        repaired = raw_text
        if not repaired.endswith("}"):
            open_braces = repaired.count("{") - repaired.count("}")
            open_brackets = repaired.count("[") - repaired.count("]")
            repaired += "}" * max(0, open_braces)
            repaired += "]" * max(0, open_brackets)
        return json.loads(repaired)


def _request_storyboard(messages: list[dict], model: str, client: anthropic.Anthropic, system_prompt: str) -> Storyboard:
    norm_model = normalize_claude_model(model)
    # Prefer Sonnet 3.5 first if norm_model is 404-prone (like opus)
    models_to_try = [norm_model]
    fallbacks = [
        "claude-3-5-sonnet-20241022",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20240620",
        "claude-3-5-haiku-20241022",
        "claude-3-haiku-20240307",
        "claude-3-opus-20240229"
    ]
    for fb in fallbacks:
        if fb not in models_to_try:
            models_to_try.append(fb)

    last_exc = None
    for m in models_to_try:
        try:
            print(f"Trying script draft with model: {m}...")
            import copy
            fallback_messages = copy.deepcopy(messages)
            json_instruction = (
                "\n\nIMPORTANT: You must respond ONLY with a raw, valid JSON object matching the requested schema. "
                "Do not wrap it in any explanation, and do not use markdown code block backticks. "
                "Start your response with { and end it with }."
            )
            if fallback_messages and fallback_messages[-1]["role"] == "user":
                fallback_messages[-1]["content"] += json_instruction

            response = client.messages.create(
                model=m,
                max_tokens=8192,
                system=system_prompt,
                messages=fallback_messages,
            )
            raw_text = next(b.text for b in response.content if b.type == "text").strip()
            data = _parse_json_robust(raw_text)
            return _beats_to_storyboard(data)
        except Exception as exc:
            exc_str = str(exc).lower()
            print(f"Script draft model {m} failed: {exc}")
            last_exc = exc
            if any(k in exc_str for k in ["not_found", "not found", "404", "invalid_request_error", "model", "permission"]):
                continue
            if isinstance(exc, (json.JSONDecodeError, KeyError, AttributeError)):
                continue
            raise exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Failed to call Anthropic API models.")


def generate_script(
    topic: str,
    num_beats: int | None = None,
    channel: str = "bestiary",
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> Storyboard:
    client = client or anthropic.Anthropic()
    system_prompt = get_system_prompt(channel)
    if channel == "calluses":
        user_prompt = (
            "Research and script a By the Calluses documentary episode, in Vesper's "
            "voice, starting with the archival cold open and following the Calluses "
            f"Codex format.\n\nHistorical topic / industry:\n{topic}\n\n{_scope(num_beats)}"
        )
    else:
        user_prompt = (
            "Research and script an Illuminated Bestiary documentary episode, in Vesper's "
            "voice, starting with the manuscript cold open and following the Illuminated "
            f"Codex format.\n\nEntity / topic:\n{topic}\n\n{_scope(num_beats)}"
        )
    sb = _request_storyboard([{"role": "user", "content": user_prompt}], model, client, system_prompt=system_prompt)
    sb.channel = channel
    return sb


def generate_script_from_messages(
    messages: list[dict],
    num_beats: int | None = None,
    channel: str = "bestiary",
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> Storyboard:
    client = client or anthropic.Anthropic()
    if not messages:
        raise ValueError("Cannot script from an empty conversation.")
    system_prompt = get_system_prompt(channel)
    if channel == "calluses":
        instruction = (
            "Now turn everything we discussed into the finished piece. Write the full "
            "By the Calluses documentary storyboard in Vesper's voice, starting "
            f"with the archival cold open and following the Calluses Codex format. "
            f"{_scope(num_beats)}"
        )
    else:
        instruction = (
            "Now turn everything we discussed into the finished piece. Write the full "
            "Illuminated Bestiary documentary storyboard in Vesper's voice, starting "
            f"with the manuscript cold open and following the Illuminated Codex format. "
            f"{_scope(num_beats)}"
        )
    convo = list(messages) + [{
        "role": "user",
        "content": instruction,
    }]
    sb = _request_storyboard(convo, model, client, system_prompt=system_prompt)
    sb.channel = channel
    return sb


def lock_script(storyboard: Storyboard) -> Storyboard:
    if not storyboard.shots:
        raise ValueError("Cannot lock: storyboard has no beats.")
    missing = [s.scene_id for s in storyboard.shots if not s.narration.strip()]
    if missing:
        raise ValueError(f"Cannot lock: beats missing narration: {missing}")
    storyboard.script_locked = True
    return storyboard
