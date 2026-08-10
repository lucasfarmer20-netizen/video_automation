"""Narrator casting — audition voices against one real passage, then choose.

The current narrator is a single voice id with process-level settings, and the
complaint is about the voice itself: a forced gravelly, affected
serious-narrator quality. That is not a settings problem, so this module does not
try to tune its way out of it. It auditions *identities*.

Two sources, one passage, identical settings:

* **Shared Voice Library** — searched through the API rather than by pasting
  voice ids found somewhere. Ids rot, names change, and a hardcoded id that no
  longer resolves fails at narration time rather than here.
* **Voice Design** — custom candidates from two written directions. These are
  previews only. ``audio.design_voice_previews`` already declines to save, and
  nothing here promotes a preview into a real voice; that happens after a human
  has listened, via ``audio.save_designed_voice``.

Everything is generated with the same text and the same settings, because the
question being asked is "which of these people do I want", and any difference in
passage or configuration between candidates makes that question unanswerable.

**No automatic selection.** There is no scoring, no ranking by an LLM, no
"recommended" flag except where a candidate is technically disqualified. The
problem is subjective preference and the human is the authority on it.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import audio, config

# --- audition settings ---------------------------------------------------------
#
# Centralised, per the brief, rather than inlined at each generation call.
#
# Deliberately NOT the production Vesper values (stability 0.35, style 0.35).
# Style exaggeration pushes a voice toward its own performed character, which is
# exactly the affectation being auditioned away from — and a high setting would
# flatter dramatic voices and penalise the natural ones. Neutral settings let the
# timbre be heard rather than the performance.

AUDITION_SETTINGS: dict = {
    "stability": float(os.environ.get("AUDITION_STABILITY", "0.5")),
    "similarity_boost": float(os.environ.get("AUDITION_SIMILARITY", "0.75")),
    "style": float(os.environ.get("AUDITION_STYLE", "0.0")),
    "use_speaker_boost": True,
}

# First pass compares voice identity, not synthesis models. v2 vs v3 is a
# separate experiment and mixing them here would confound both.
AUDITION_MODEL = os.environ.get("AUDITION_MODEL", "eleven_multilingual_v2")

SHARED_VOICES_URL = "https://api.elevenlabs.io/v1/shared-voices"

# Named candidates worth trying to resolve. Names, not ids: the brief supplies
# names, and any id pasted from elsewhere is unverifiable and may already be
# stale. A name that no longer resolves is reported, never fatal.
SHORTLIST = [
    "David — American Narrator",
    "Jonathan — Sophisticated, Calm Narrator",
    "A Top Narrator VO PRO",
    "Ian Cartwell — Mystery Storyteller",
    "Frederick Surrey — Science Storyteller",
    "Bill — Informative, Clean and Natural",
]

# Search terms for the library sweep. Ranked toward the creative target and away
# from the qualities being rejected.
SEARCH_TERMS = ["narration", "documentary", "storytelling", "conversational",
                "calm narrator", "informative"]

REJECT_WORDS = {"gravelly", "raspy", "rasp", "booming", "trailer", "epic",
                "dramatic", "announcer", "deep bass", "growl", "promo",
                "commercial", "gritty"}

VOICE_DESIGNS: dict[str, str] = {
    "grounded": (
        "Native American English. Male, 42-55. Studio quality. Persona: experienced "
        "documentary storyteller. Emotion: grounded, curious, restrained. Natural "
        "mid-low register with clear resonance but no rasp, vocal fry, gravel, or "
        "exaggerated bass. Conversational and intelligent rather than announcer-like. "
        "Measured natural pacing with subtle variation and understated authority. "
        "Sounds like a real person telling an extraordinary true story, not "
        "performing a movie trailer."
    ),
    "contemporary": (
        "Native American English. Male, 35-48. Studio quality. Persona: thoughtful "
        "historical storyteller. Emotion: calm, engaged, quietly confident. Clean "
        "natural timbre in a comfortable middle register, warm without sounding dark "
        "or gravelly. Relaxed conversational delivery with crisp articulation and "
        "organic pacing. Subtle wonder and tension when appropriate, but never "
        "theatrical, booming, breathy, raspy, or promotional."
    ),
}


def auditions_dir() -> Path:
    """Auditions live beside the project, not inside an episode's audio."""
    return config.MANIFEST_PATH.parent / "vo_auditions"


# --- the audition passage ------------------------------------------------------

_WPM = 150.0        # unhurried documentary read


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9'’-]+", text or ""))


def _qualities(text: str) -> dict:
    """Which delivery challenges a passage actually contains."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]
    lengths = [_words(s) for s in sentences]
    # A capitalised word that is not sentence-initial is a decent proper-noun proxy.
    proper = bool(re.search(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}", text or "", re.M))
    numbers = bool(re.search(r"\b\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|"
                             r"hundred|thousand|million)\b", text or "", re.I))
    return {
        "words": _words(text),
        "seconds": round(_words(text) / _WPM * 60.0, 1),
        "sentences": len(sentences),
        "proper_noun": proper,
        "numbers": numbers,
        # A short sentence beside a long one is what makes a read reveal itself.
        "varied_lengths": bool(lengths and (max(lengths) - min(lengths) >= 6)),
    }


def pick_passage(sb, min_seconds: float = 25.0, max_seconds: float = 40.0) -> dict:
    """Choose one real narration passage that exercises the voice.

    Deliberately selects from written narration rather than composing something:
    an invented paragraph would audition the candidates against words the channel
    will never speak. Raises when nothing qualifies, so the caller stops and asks
    rather than quietly settling for a weak passage.
    """
    lo, hi = min_seconds / 60.0 * _WPM, max_seconds / 60.0 * _WPM
    scored = []
    for shot in getattr(sb, "shots", []):
        text = (shot.narration or "").strip()
        if not text:
            continue
        q = _qualities(text)
        if not (lo <= q["words"] <= hi):
            continue
        score = (q["proper_noun"] + q["numbers"] + q["varied_lengths"]
                 + (q["sentences"] >= 3))
        scored.append((score, shot.scene_id, text, q))

    if not scored:
        raise RuntimeError(
            f"No single beat in '{getattr(sb, 'title', '?')}' is between "
            f"{min_seconds:.0f}s and {max_seconds:.0f}s of narration "
            f"({int(lo)}-{int(hi)} words). Supply an audition paragraph rather "
            f"than having one invented — this text is what every candidate is "
            f"judged on."
        )

    scored.sort(key=lambda r: (-r[0], r[1]))
    score, scene_id, text, q = scored[0]
    return {"scene_id": scene_id, "text": text, "score": score, "qualities": q,
            "alternatives": [{"scene_id": s[1], "score": s[0], "seconds": s[3]["seconds"]}
                             for s in scored[1:5]]}


# --- shared voice library ------------------------------------------------------

def _looks_rejected(v: dict) -> list[str]:
    blob = " ".join(str(v.get(k) or "") for k in
                    ("name", "description", "use_case", "descriptive")).lower()
    labels = " ".join(f"{k}:{x}" for k, x in (v.get("labels") or {}).items()).lower()
    return sorted({w for w in REJECT_WORDS if w in blob or w in labels})


def _normalise(v: dict) -> dict:
    labels = v.get("labels") or {}
    return {
        "voice_id": v.get("voice_id") or v.get("public_owner_id") or "",
        "public_owner_id": v.get("public_owner_id") or "",
        "name": v.get("name") or "",
        "description": (v.get("description") or v.get("descriptive") or "")[:400],
        "labels": labels,
        "category": v.get("category") or "",
        "preview_url": v.get("preview_url") or "",
        "accent": v.get("accent") or labels.get("accent") or "",
        "age": v.get("age") or labels.get("age") or "",
        "gender": v.get("gender") or labels.get("gender") or "",
        "use_case": v.get("use_case") or labels.get("use_case") or "",
        "language": v.get("language") or labels.get("language") or "",
        "rejected_for": _looks_rejected(v),
    }


def search_library(terms: list[str] | None = None, page_size: int = 30,
                   log=print) -> list[dict]:
    """Sweep the shared library for narration-suitable English male voices.

    One request per search term, deduplicated by voice_id. Filters are sent as
    query parameters and a failing term is skipped rather than aborting the
    sweep — the library API's accepted parameters have changed before, and losing
    one term is far better than losing the audition.
    """
    import requests

    found: dict[str, dict] = {}
    for term in (terms or SEARCH_TERMS):
        params = {
            "page_size": page_size, "search": term,
            "gender": "male", "language": "en", "use_cases": "narrative_story",
        }
        try:
            r = requests.get(SHARED_VOICES_URL, headers=_headers(),
                             params=params, timeout=60)
            r.raise_for_status()
            voices = r.json().get("voices") or []
        except Exception as exc:  # noqa: BLE001
            log(f"  library search {term!r} failed ({exc}) — skipped")
            continue
        new = 0
        for v in voices:
            n = _normalise(v)
            if n["voice_id"] and n["voice_id"] not in found:
                found[n["voice_id"]] = n
                new += 1
        log(f"  {term!r}: {len(voices)} returned, {new} new")
    return list(found.values())


def resolve_shortlist(names: list[str] | None = None, log=print) -> dict:
    """Try to find the named candidates. Missing names are reported, not fatal."""
    import requests

    out: dict[str, dict] = {}
    for name in (names or SHORTLIST):
        # Match on the distinctive leading token; the full display names carry
        # em-dashes and suffixes that a search index may not reproduce.
        head = re.split(r"[—\-–,]", name)[0].strip()
        try:
            r = requests.get(SHARED_VOICES_URL, headers=_headers(),
                             params={"search": head, "page_size": 30,
                                     "gender": "male", "language": "en"},
                             timeout=60)
            r.raise_for_status()
            voices = r.json().get("voices") or []
        except Exception as exc:  # noqa: BLE001
            log(f"  shortlist {name!r}: search failed ({exc})")
            continue
        hit = next((v for v in voices
                    if head.lower() in (v.get("name") or "").lower()), None)
        if hit:
            out[name] = _normalise(hit)
            log(f"  shortlist {name!r} -> {hit.get('name')} ({out[name]['voice_id']})")
        else:
            log(f"  shortlist {name!r}: not found in the library")
    return out


def _headers() -> dict:
    config.require_for("audio")
    return {"xi-api-key": config.ELEVENLABS_API_KEY}


# --- generation ----------------------------------------------------------------

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48] or "voice"


def _speak(voice_id: str, text: str, dest: Path) -> Path:
    """One audition line, at the shared settings. Same path production uses."""
    from elevenlabs import VoiceSettings

    client = audio._client()
    dest.parent.mkdir(parents=True, exist_ok=True)
    stream = client.text_to_speech.convert(
        voice_id=voice_id, text=text, model_id=AUDITION_MODEL,
        output_format=audio.OUTPUT_FORMAT,
        voice_settings=VoiceSettings(**AUDITION_SETTINGS),
    )
    return audio._write_stream(stream, dest)


def run_audition(sb, library_limit: int = 8, passage: str = "",
                 include_designed: bool = True, log=print) -> dict:
    """Generate the full audition set and write the manifest. Selects nothing."""
    out_dir = auditions_dir()
    (out_dir / "library").mkdir(parents=True, exist_ok=True)
    (out_dir / "designed").mkdir(parents=True, exist_ok=True)

    if (passage or "").strip():
        text = passage.strip()
        source = {"scene_id": "(supplied)", "qualities": _qualities(text)}
    else:
        source = pick_passage(sb)
        text = source["text"]
    log(f"Audition passage: {source.get('scene_id')} — {_words(text)} words, "
        f"~{_qualities(text)['seconds']:.0f}s")

    candidates: list[dict] = []
    issues: list[str] = []

    # 1. Named shortlist first, then fill from the search sweep.
    shortlist = resolve_shortlist(log=log)
    for name in SHORTLIST:
        if name not in shortlist:
            issues.append(f"shortlist candidate not found in the library: {name}")

    pool = list(shortlist.values())
    seen = {v["voice_id"] for v in pool}
    for v in search_library(log=log):
        if v["voice_id"] in seen:
            continue
        if v["rejected_for"]:
            continue          # matches the qualities being auditioned away from
        pool.append(v)
        seen.add(v["voice_id"])
        if len(pool) >= library_limit:
            break

    log(f"{len(pool)} library candidate(s) to render")
    for v in pool:
        dest = out_dir / "library" / f"{_slug(v['name'])}.mp3"
        try:
            _speak(v["voice_id"], text, dest)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{v['name']}: TTS failed ({exc})")
            log(f"  !! {v['name']}: {exc}")
            continue
        candidates.append({
            "id": _slug(v["name"]), "source": "library",
            "voice_id": v["voice_id"], "name": v["name"],
            "description": v["description"], "labels": v["labels"],
            "category": v["category"], "accent": v["accent"], "age": v["age"],
            "use_case": v["use_case"], "preview_url": v["preview_url"],
            "audio": str(dest.relative_to(out_dir)).replace("\\", "/"),
            "settings": dict(AUDITION_SETTINGS), "model_id": AUDITION_MODEL,
        })
        log(f"  rendered {v['name']}")

    # 2. Voice Design. Previews only — nothing is saved to the account.
    if include_designed:
        for key, description in VOICE_DESIGNS.items():
            try:
                result = audio.design_voice_previews(description=description,
                                                     sample_text=text)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"voice design {key!r} failed ({exc})")
                log(f"  !! design {key}: {exc}")
                continue
            for i, pv in enumerate(result.get("previews") or [], start=1):
                uri = pv.get("audio_data_uri") or ""
                if "," not in uri:
                    issues.append(f"design {key} preview {i}: no audio returned")
                    continue
                dest = out_dir / "designed" / f"{key}_{i:02d}.mp3"
                dest.write_bytes(base64.b64decode(uri.split(",", 1)[1]))
                candidates.append({
                    "id": f"{key}_{i:02d}", "source": "designed",
                    "generated_voice_id": pv.get("generated_voice_id", ""),
                    "voice_id": "",              # not usable until saved
                    "name": f"{key.title()} #{i}",
                    "design_prompt": description,
                    "duration_secs": pv.get("duration_secs"),
                    "audio": str(dest.relative_to(out_dir)).replace("\\", "/"),
                    "settings": dict(AUDITION_SETTINGS), "model_id": AUDITION_MODEL,
                })
                log(f"  rendered designed {key} #{i}")

    manifest = {
        "audition_text": text,
        "passage_source": source.get("scene_id"),
        "passage_qualities": source.get("qualities"),
        "model_id": AUDITION_MODEL,
        "settings": dict(AUDITION_SETTINGS),
        "project": getattr(sb, "title", ""),
        "candidates": candidates,
        "issues": issues,
        "note": ("Designed candidates are previews. A generated_voice_id cannot be "
                 "used for narration until it is promoted via "
                 "audio.save_designed_voice()."),
    }
    (out_dir / "audition_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Wrote {len(candidates)} candidate(s) to {out_dir}")
    return manifest


# --- VO profiles ---------------------------------------------------------------
#
# Only the settings our TTS call actually sends. `_voice_settings` builds
# VoiceSettings(stability, similarity_boost, style, use_speaker_boost) -- there is
# no speed field in that call, so a profile carrying one would be silently
# ignored, which is worse than not offering it.

@dataclass
class VOProfile:
    id: str
    name: str
    voice_id: str = ""
    model_id: str = AUDITION_MODEL
    provider: str = "elevenlabs"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
    note: str = ""

    def settings(self) -> dict:
        return {"stability": self.stability, "similarity_boost": self.similarity_boost,
                "style": self.style, "use_speaker_boost": self.use_speaker_boost}


def profiles_path() -> Path:
    """Cross-project, beside audio_pool — a narrator outlives one episode."""
    mount = Path("/gcs")
    base = mount if mount.exists() else config.ROOT
    return base / "vo_profiles.json"


def load_profiles() -> dict[str, VOProfile]:
    p = profiles_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for pid, spec in (raw or {}).items():
        spec = dict(spec or {}); spec["id"] = pid
        spec.setdefault("name", pid)
        out[pid] = VOProfile(**{k: v for k, v in spec.items()
                                if k in VOProfile.__dataclass_fields__})
    return out


def save_profile(profile: VOProfile) -> Path:
    p = profiles_path()
    raw = {}
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            raw = {}
    raw[profile.id] = asdict(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return p


def resolve_profile(sb) -> VOProfile | None:
    """The profile for this episode, or None to keep current behaviour.

    Backward compatibility is the point: an episode that names no profile, or
    names one that does not exist, resolves to None and narration falls through
    to exactly the path it uses today. Nothing is migrated onto a new narrator.
    """
    key = (getattr(sb, "vo_profile", "") or "").strip()
    if not key:
        return None
    return load_profiles().get(key)
