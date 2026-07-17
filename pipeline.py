"""Single orchestrator entrypoint.

Routes to modular code in ``src/`` and enforces the hard human-approval gates:
the script gate (a human locks the narration before any paid stage runs) and the
storyboard/budget gate (the pipeline pauses after draft-image generation,
launches the Flask dashboard, and refuses to call any paid video API until an
approved ``storyboard_manifest.json`` has been written).

This file *orchestrates only* — all domain logic lives in ``src/`` (see
CLAUDE.md). The run is a resumable state machine driven by the manifest flags:
each invocation advances as far as it can, then **pauses at the next human gate**
and exits. Re-run to continue once the human has acted.

CLI:
    python pipeline.py --topic "The manananggal of Barrio Consuelo"
    python pipeline.py                 # resume from the current manifest state
"""

from __future__ import annotations

import argparse

import os

from src import config
from src.manifest import Shot, Storyboard, load, save

CHANNEL_TITLE = "The Illuminated Bestiary"
DRAFTS_PER_SHOT = 3
DEFAULT_SHOT_SECONDS = 6.0  # fallback when a shot carries no camera duration

# --------------------------------------------------------------------------- #
# Cloud-Native Path Setup & Directory Assurance
# --------------------------------------------------------------------------- #
# Use Cloud Storage FUSE path when running in Cloud Run, otherwise fall back to local file
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "./storyboard_manifest.json")
ASSETS_DIR = os.environ.get("ASSETS_DIR", "./static/assets")

# Ensure the cloud directories exist dynamically on boot so Python doesn't crash
os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def _init_storyboard() -> Storyboard:
    """Load the manifest, or start a fresh Storyboard for the channel.

    ``load`` yields a blank ``Storyboard`` when no manifest exists; we stamp the
    channel title onto that blank state and persist it so the file exists from
    the first run.
    """
    sb = load()
    if not sb.shots and not sb.title:
        sb.title = CHANNEL_TITLE
        save(sb)
        print(f'Initialized new manifest for "{CHANNEL_TITLE}" -> {config.MANIFEST_PATH}')
    return sb


# --------------------------------------------------------------------------- #
# stage 1 — script (drafts, then pauses at the script gate)
# --------------------------------------------------------------------------- #
def stage_script(sb: Storyboard, topic: str | None, num_beats: int | None) -> None:
    """Draft the narration/beats if needed, then pause for the human to lock.

    Never auto-locks: CLAUDE.md's script gate requires a human to refine and
    approve the narration before anything downstream may consume it.
    """
    from src import script

    if not sb.shots:
        if not topic:
            print(
                "Script stage: the manifest has no beats and no --topic was given.\n"
                "Provide a topic to draft a script, e.g.:\n"
                '    python pipeline.py --topic "The Leshy of the birch woods"'
            )
            return
        print(f"Drafting script for topic: {topic!r} ...")
        draft = script.generate_script(topic, num_beats=num_beats)
        # Keep the channel identity on the manifest title (per channel spec).
        draft.title = CHANNEL_TITLE
        sb = draft
        save(sb)
        print(f"Drafted {len(sb.shots)} beats -> {config.MANIFEST_PATH}")

    print("\n== SCRIPT GATE ==")
    print("Refine the narration/visuals in the manifest, then lock the script:")
    print("    python -m src.script --lock")
    print("Re-run the pipeline once the script is locked to continue.")


# --------------------------------------------------------------------------- #
# stage 2 — audio + rhythm landmarks
# --------------------------------------------------------------------------- #
def _landmarks(track: str) -> list[float]:
    """Sorted, de-duplicated cut candidates from beats + percussive transients."""
    from src import audio

    analysis = audio.analyze_music(track)
    transients = audio.detect_transients(track)
    return sorted(set(analysis.get("beats", [])) | set(transients))


def _map_anchors(shots: list[Shot], landmarks: list[float]) -> int:
    """Snap each shot's running start time to the nearest unused landmark.

    Heuristic, monotonic, non-repeating: we walk the cumulative per-shot camera
    duration as the *intended* start of each beat and snap it to the closest
    remaining beat/transient, so cuts land on the music without two beats sharing
    an anchor. Returns the count of shots anchored.
    """
    if not landmarks:
        return 0
    t = 0.0
    start = 0
    anchored = 0
    for shot in shots:
        best_i: int | None = None
        best_d = float("inf")
        for i in range(start, len(landmarks)):
            d = abs(landmarks[i] - t)
            if d < best_d:
                best_d, best_i = d, i
            elif landmarks[i] > t:
                break  # distance only grows once we pass t
        if best_i is None:
            break
        shot.audio_anchor = landmarks[best_i]
        anchored += 1
        start = best_i + 1
        t += shot.camera.duration if shot.camera else DEFAULT_SHOT_SECONDS
    return anchored


def stage_audio(sb: Storyboard) -> Storyboard:
    """Synthesize narration, then anchor each cut to a music landmark."""
    from src import audio

    print("\n== AUDIO & LANDMARK STAGE ==")
    clips = audio.synthesize_narration(sb)  # paid; enforces the script gate itself
    print(f"Narration: {len(clips)} clip(s).")

    if sb.music_track:
        landmarks = _landmarks(sb.music_track)
        anchored = _map_anchors(sb.shots, landmarks)
        print(f"Anchored {anchored}/{len(sb.shots)} beats to {len(landmarks)} music landmarks.")
    else:
        print(
            "No music_track set — skipping audio_anchor mapping. "
            "Drop a WAV/MP3 in audio_pool/ and set music_track to anchor cuts."
        )

    save(sb)
    return sb


# --------------------------------------------------------------------------- #
# stage 3 — visual drafts
# --------------------------------------------------------------------------- #
def stage_drafts(sb: Storyboard) -> Storyboard:
    """Generate draft image variations for every beat (default backend)."""
    from src import assets

    print("\n== VISUAL DRAFT STAGE ==")
    assets.generate_drafts(
        sb,
        n=DRAFTS_PER_SHOT,
        backend=assets.DEFAULT_BACKEND,
        skip_existing=True,
        save_after_each=True,
    )
    save(sb)
    return sb


# --------------------------------------------------------------------------- #
# stage 4 — human-approval gate (Gate 1)
# --------------------------------------------------------------------------- #
def stage_gate(sb: Storyboard, host: str, port: int) -> Storyboard | None:
    """Launch the storyboard/budget dashboard and block until the gate clears.

    Returns the approved storyboard once ``gate_cleared()`` is true, or ``None``
    if the human has not finished approving — in which case the caller must not
    proceed to any paid render stage.
    """
    from src import dashboard

    if sb.gate_cleared():
        print("\nGate already cleared.")
        return sb

    print("\n== HUMAN-APPROVAL GATE (Gate 1) ==")
    print("Launching the storyboard / budget dashboard. In the browser:")
    print(f"  1. Open http://{host}:{port}")
    print("  2. For each beat: pick a draft image and set its motion tier.")
    print("  3. Click Approve to write the approved manifest.")
    print("  4. Then stop this server (Ctrl+C) to resume the pipeline.")
    print("No paid video API will run until the gate is cleared.\n")

    try:
        dashboard.run(host=host, port=port)
    except KeyboardInterrupt:
        print("\nDashboard stopped — checking the gate ...")

    sb = load()  # reload whatever the human approved in the UI
    if not sb.gate_cleared():
        print(
            "Gate NOT cleared: the storyboard is not fully approved (every beat "
            "needs a chosen image, and every ai_video beat a model). Re-run and "
            "complete approval before any render stage."
        )
        return None
    print("Gate cleared — the storyboard is approved.")
    return sb


# --------------------------------------------------------------------------- #
# control flow
# --------------------------------------------------------------------------- #
def run_pipeline(
    topic: str | None = None,
    num_beats: int | None = None,
    host: str = "127.0.0.1",
    port: int = 5000,
) -> None:
    """Advance the run through stages 1-4, pausing at each human gate."""
    sb = _init_storyboard()

    # Stage 1 — script gate. Runs (and pauses) until the script is locked.
    if not sb.script_locked:
        stage_script(sb, topic=topic, num_beats=num_beats)
        return

    # Stage 2 — narration + rhythm landmarks.
    sb = stage_audio(sb)

    # Stage 3 — draft images.
    sb = stage_drafts(sb)

    # Stage 4 — storyboard/budget gate. Blocks on human approval.
    if stage_gate(sb, host=host, port=port) is None:
        return

    # Stages 5+ (paid video render, parallax render, timeline/FCPXML — Gate 2)
    # are intentionally out of scope for this task and not yet implemented.
    print("\nGate cleared. Next: render + timeline stages (Gate 2) — not yet implemented.")


def _main() -> None:
    parser = argparse.ArgumentParser(description="The Illuminated Bestiary master orchestrator.")
    parser.add_argument("--topic", help="Episode topic/brief (only needed to draft a new script).")
    parser.add_argument("--beats", type=int, default=None, help="Approx beat count when drafting.")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard host.")
    parser.add_argument("--port", type=int, default=5000, help="Dashboard port.")
    args = parser.parse_args()
    run_pipeline(topic=args.topic, num_beats=args.beats, host=args.host, port=args.port)


if __name__ == "__main__":
    _main()
