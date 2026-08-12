"""Publishing metadata for a finished episode: title, description, chapters, tags.

Generated from the locked script with the same structured-output call shape as
``script.py`` — ``output_config.format`` enforces the schema server-side, so the
response is valid JSON in the right shape with no fences to strip and no retry
loop re-billing a full generation on a parse failure.

Chapter timestamps are **computed from the manifest**, never asked for from the
model: the beat durations are already known exactly, and a model asked to invent
timecodes will produce plausible ones that do not match the cut.

Publishing itself is not part of this pipeline (see CLAUDE.md, Gate 2). This
produces text for a human to paste.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from . import config
from .manifest import Storyboard, load

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8000

# YouTube's own limits. Exceeding them means the paste silently truncates.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500          # combined character budget across all tags


METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                f"YouTube title, at most {TITLE_MAX} characters. Documentary "
                "register — no clickbait, no ALL CAPS, no emoji."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "YouTube description. Two or three short paragraphs of genuine "
                "context about the entity and its culture of origin, then a line "
                "crediting the art medium. Do NOT include chapter timestamps — "
                "they are appended from the manifest."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "8-15 lowercase search tags, no '#', no duplicates.",
        },
        "chapter_titles": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One short chapter title per beat, in order, same count as the "
                "beats given. 2-5 words each. These are labels only — timestamps "
                "are computed, not supplied."
            ),
        },
        "thumbnail_prompts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-3 image prompts for thumbnail candidates, in the episode's "
                "own cultural art medium. Each must describe a single arresting "
                "subject that reads at 320px wide."
            ),
        },
    },
    "required": ["title", "description", "tags", "chapter_titles"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You write publishing metadata for The Illuminated Bestiary, a
documentary folklore-horror channel narrated by Vesper.

Register: archival, restrained, genuinely curious about the culture. The channel
treats each entity as a historical and cultural artefact, not a monster-of-the-week.

Hard rules:
- Never use clickbait constructions: no "you won't believe", no "the truth about",
  no ALL CAPS words, no emoji, no exclamation marks.
- Never claim the folklore is literally true, and never claim it is merely
  superstition. Report what the sources say and who said it.
- Name the culture of origin plainly and respectfully.
- The description must not contain timestamps; they are added from the manifest.
"""


@dataclass
class Chapter:
    start: float
    title: str

    @property
    def timestamp(self) -> str:
        m, s = divmod(int(round(self.start)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Metadata:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    thumbnail_prompts: list[str] = field(default_factory=list)

    def description_with_chapters(self) -> str:
        """Description plus a YouTube-parseable chapter list.

        YouTube only creates chapters when the first entry is 0:00 and there are
        at least three of them, so a short episode simply gets no chapter block
        rather than a broken one.
        """
        if len(self.chapters) < 3 or not self.chapters or self.chapters[0].start > 0.5:
            return self.description
        lines = "\n".join(f"{c.timestamp} {c.title}" for c in self.chapters)
        return f"{self.description}\n\nChapters:\n{lines}"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "description_with_chapters": self.description_with_chapters(),
            "tags": self.tags,
            "tags_chars": sum(len(t) for t in self.tags) + max(0, len(self.tags) - 1),
            "chapters": [{"start": c.start, "timestamp": c.timestamp, "title": c.title}
                         for c in self.chapters],
            "thumbnail_prompts": self.thumbnail_prompts,
            "title_chars": len(self.title),
            "warnings": self.warnings(),
        }

    def warnings(self) -> list[str]:
        """Anything that would be silently truncated or rejected on paste."""
        w: list[str] = []
        if len(self.title) > TITLE_MAX:
            w.append(f"Title is {len(self.title)} chars; YouTube truncates at {TITLE_MAX}.")
        if len(self.description_with_chapters()) > DESCRIPTION_MAX:
            w.append(f"Description is {len(self.description_with_chapters())} chars; "
                     f"YouTube truncates at {DESCRIPTION_MAX}.")
        tag_chars = sum(len(t) for t in self.tags) + max(0, len(self.tags) - 1)
        if tag_chars > TAGS_TOTAL_MAX:
            w.append(f"Tags total {tag_chars} chars; YouTube's combined limit is {TAGS_TOTAL_MAX}.")
        if len(self.chapters) < 3:
            w.append("Fewer than 3 chapters — YouTube will not show a chapter list.")
        return w

    @classmethod
    def from_dict(cls, d: dict) -> "Metadata":
        return cls(
            title=d.get("title", "") or "",
            description=d.get("description", "") or "",
            tags=list(d.get("tags") or []),
            chapters=[Chapter(start=float(c.get("start", 0.0)), title=c.get("title", ""))
                      for c in (d.get("chapters") or [])],
            thumbnail_prompts=list(d.get("thumbnail_prompts") or []),
        )


def _client(client: anthropic.Anthropic | None = None) -> anthropic.Anthropic:
    if client is not None:
        return client
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return anthropic.Anthropic(api_key=key)


def beat_offsets(sb: Storyboard) -> list[float]:
    """Cumulative start time of each beat, from the manifest's own durations."""
    offsets, acc = [], 0.0
    for s in sb.shots:
        offsets.append(acc)
        acc += float(s.camera.duration) if s.camera else 6.0
    return offsets


def generate(storyboard: Storyboard | None = None,
             client: anthropic.Anthropic | None = None,
             model: str = DEFAULT_MODEL,
             log=print) -> Metadata:
    """Draft publishing metadata for the episode. Requires a locked script."""
    sb = storyboard or load()
    if not sb.shots:
        raise RuntimeError("This project has no beats yet.")
    if not sb.script_locked:
        raise RuntimeError(
            "Script gate: metadata is drafted from the locked script. Lock it first."
        )

    beats = [
        {
            "scene_id": s.scene_id,
            "narration": (s.narration or "").strip(),
            "seconds": round(float(s.camera.duration) if s.camera else 6.0, 1),
        }
        for s in sb.shots
    ]
    runtime = sum(b["seconds"] for b in beats)

    user = (
        f"Episode title (working): {sb.title}\n"
        f"Channel: {sb.channel}\n"
        f"Culture of origin: {sb.cultural_origin or 'unspecified'}\n"
        f"Runtime: {runtime/60:.1f} minutes across {len(beats)} beats\n\n"
        "Beats, in order:\n"
        + "\n".join(f"{i+1}. [{b['seconds']}s] {b['narration']}" for i, b in enumerate(beats))
        + f"\n\nWrite the publishing metadata. Give exactly {len(beats)} chapter titles, "
          "one per beat, in the same order."
    )

    log(f"Requesting episode metadata from Claude ({model}) ...")
    with _client(client).messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": METADATA_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this metadata request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Metadata hit the {MAX_TOKENS}-token ceiling and was cut off."
        )

    import json
    data = json.loads(next(b.text for b in response.content if getattr(b, "text", None)))

    # Timestamps come from the manifest, not the model. Titles are zipped onto
    # the real offsets, so a short or long title list cannot desynchronise them.
    offsets = beat_offsets(sb)
    titles = list(data.get("chapter_titles") or [])
    if len(titles) != len(offsets):
        log(f"metadata: model returned {len(titles)} chapter titles for "
            f"{len(offsets)} beats; zipping to the shorter of the two.")
    chapters = [Chapter(start=o, title=t) for o, t in zip(offsets, titles)]

    md = Metadata(
        title=(data.get("title") or "").strip(),
        description=(data.get("description") or "").strip(),
        tags=[t.strip().lstrip("#").lower() for t in (data.get("tags") or []) if t.strip()],
        chapters=chapters,
        thumbnail_prompts=list(data.get("thumbnail_prompts") or []),
    )
    for w in md.warnings():
        log(f"metadata: {w}")
    return md


def path_for(sb: Storyboard) -> "os.PathLike":
    """Where this episode's metadata JSON lives — beside its manifest."""
    from pathlib import Path
    return config.project_dir() / "metadata.json"


def save(md: Metadata, sb: Storyboard | None = None) -> "os.PathLike":
    import json
    from pathlib import Path
    sb = sb or load()
    p = Path(path_for(sb))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(md.to_dict(), indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
    return p


def load_saved(sb: Storyboard | None = None) -> Metadata | None:
    import json
    from pathlib import Path
    sb = sb or load()
    p = Path(path_for(sb))
    if not p.is_file():
        return None
    try:
        return Metadata.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — a corrupt sidecar must not break the studio
        print(f"metadata: could not read {p} ({exc}); treating as absent.")
        return None
