"""FastAPI Application entry point for the YouTube Video Automation Studio.

Routes requests to the Firestore database models, ElevenLabs audio generator,
fal.ai media API, and DaVinci Resolve OTIO exporter.
"""

from __future__ import annotations

import base64
import os
import re
import json
import shutil
import subprocess
import tempfile
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

import fal_client
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import unicodedata

def secure_filename(filename: str) -> str:
    """A pure-python replacement for werkzeug.utils.secure_filename."""
    filename = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    seps = [os.path.sep]
    if getattr(os, "altsep", None):
        seps.append(os.path.altsep)
    for sep in seps:
        if sep:
            filename = filename.replace(sep, "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename).strip("._")
    if not filename:
        filename = "project"
    return filename

# Submodule imports
from . import config, manifest, script, assets, audio, motion, timeline, sizzle, metadata, bundle
from .manifest import Storyboard, Shot, MotionType, Camera, RenderConfig, db
from .pipeline_worker import start_job, get_jobs_status, log_job

# The moves motion._camera() actually implements; anything else renders as a
# frozen plate, so reject it at the API rather than silently producing a still.
CAMERA_MOVES = {"static", "push_in", "push_out", "pan_left", "pan_right"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Seed missing project manifests, then bootstrap Firestore from disk.

    Both steps are additive — nothing here deletes or rewrites existing state,
    because this runs on every cold start.
    """
    try:
        ensure_gcs_projects()
    except Exception:
        print(f"Startup warning: ensure_gcs_projects failed:\n{traceback.format_exc()}")

    if db is not None:
        try:
            for p in _scan_projects():
                f_id = get_project_id_from_path(p["rel"])
                if not db.collection("projects").document(f_id).get().exists:
                    print(f"Firestore bootstrap: loading {p['name']} from disk ({f_id}) ...")
                    sb = manifest.load(Path(p["rel"]))
                    sb.id = f_id
                    manifest.save_project(sb)
        except Exception:
            print(f"Startup warning: Firestore bootstrap failed:\n{traceback.format_exc()}")
    else:
        print("Startup: Firestore unavailable — running on local JSON manifests only.")

    yield


app = FastAPI(title="YouTube Automation Studio API", lifespan=lifespan)

# CORS. The studio serves its own frontend from the same origin in production,
# so the default is same-origin only; STUDIO_ALLOWED_ORIGINS opens it up for
# local Next.js dev (e.g. "http://localhost:3000,http://localhost:5000").
# "*" with allow_credentials=True lets any site on the internet make credentialed
# calls to this API, so it is deliberately not the default.
_DEV_ORIGINS = [
    "http://localhost:3000",   # `npm run dev`
    "http://127.0.0.1:3000",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
]
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("STUDIO_ALLOWED_ORIGINS", "").split(",") if o.strip()
] or _DEV_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional shared-secret gate. Cloud Run is deployed --allow-unauthenticated, so
# without this anyone with the URL can trigger paid fal.ai renders and delete
# files. Set STUDIO_API_KEY and send it as X-Studio-Key to lock the studio down;
# leaving it unset preserves the current open behaviour.
STUDIO_API_KEY = os.environ.get("STUDIO_API_KEY", "")
_PUBLIC_PATHS = ("/media/", "/assets/", "/references/", "/render/")


@app.middleware("http")
async def require_studio_key(request: Request, call_next):
    if STUDIO_API_KEY and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Studio-Key") != STUDIO_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Missing or invalid X-Studio-Key."},
            )
    return await call_next(request)


WORKSPACE_ROOT = Path(config.ROOT).resolve()
ACTIVE_PROJECT_FILE = Path("/gcs/.active_project") if Path("/gcs").exists() else Path(".active_project")
IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "frontend", "backend"}


def get_project_id_from_path(path: str | Path) -> str:
    """Derive a clean, unique Firestore document ID from a manifest path."""
    path_str = str(path).replace("\\", "/").strip("/")
    # Replace non-alphanumeric characters with underscores
    safe_id = "".join([c if c.isalnum() else "_" for c in path_str])
    return safe_id


def get_active_manifest_path() -> str:
    """Read the active manifest path file from disk."""
    if ACTIVE_PROJECT_FILE.exists():
        p_str = ACTIVE_PROJECT_FILE.read_text(encoding="utf-8").strip()
        if p_str:
            return str(Path(p_str).resolve())
    
    # Fallback default path
    gcs_parent = Path("/gcs")
    if gcs_parent.exists():
        default_p = gcs_parent / "bestiary" / "manananggal" / "storyboard_manifest.json"
    else:
        default_p = WORKSPACE_ROOT / "bestiary" / "manananggal" / "storyboard_manifest.json"
    return str(default_p.resolve())


def set_active_manifest_path(path: str):
    """Save the active manifest path to the config file."""
    ACTIVE_PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROJECT_FILE.write_text(str(Path(path).resolve()), encoding="utf-8")


def get_current_project() -> Storyboard:
    """Retrieve the currently active storyboard manifest with robust fallback creation."""
    active_path = get_active_manifest_path()
    f_id = get_project_id_from_path(active_path)
    sb = None
    try:
        sb = manifest.load_project(f_id)
    except Exception as fe:
        print(f"Warning: Firestore load_project failed: {fe}")
        
    if not sb:
        p = Path(active_path)
        if p.exists() and p.is_file():
            try:
                sb = manifest.load(p)
                sb.id = f_id
            except Exception as le:
                print(f"Warning: Local manifest load failed: {le}")
                
    if not sb:
        p = Path(active_path)
        # NEVER overwrite a manifest that exists but failed to parse. This branch
        # used to save a fresh empty Storyboard unconditionally, so any load
        # error -- a stray field, a truncated write, a transient read -- silently
        # replaced a whole storyboard with an empty one. Seeding is only correct
        # when there is genuinely nothing there.
        if p.exists():
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Manifest at {p} exists but could not be loaded. Refusing to "
                    f"overwrite it — inspect the file or restore from _backup/."
                ),
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        sb = Storyboard(title=p.parent.name or "untitled", channel="bestiary")
        sb.id = f_id
        try:
            manifest.save(sb, p)
        except Exception as exc:
            print(f"Warning: could not seed new manifest at {p}: {exc}")


    config.set_active_manifest(active_path)
    return sb


def save_current_project(sb: Storyboard):
    """Save storyboard manifest state to both Firestore and local disk JSON."""
    # Save to Firestore
    try:
        manifest.save_project(sb)
    except Exception as fe:
        print(f"Warning: Firestore save_project failed: {fe}")
    # Save back to local/GCS JSON file for CLI & local sync
    active_path = get_active_manifest_path()
    manifest.save(sb, Path(active_path))


def _scan_projects() -> list[dict]:
    """Discover storyboard_manifest.json projects, one entry per real project.

    Scans the GCS mount when it exists and the workspace otherwise — never both.
    On Cloud Run the container also carries the repo's committed manifests
    (storyboard_manifest.json, bestiary/, calluses/), which are seed data, not
    projects; scanning both roots listed each of them a second time. That is what
    produced three Manananggal entries and a stray Leshy under Calluses. The
    previous code hid it by deleting those files on every cold start, which also
    deleted real project state.
    """
    active = Path(get_active_manifest_path()).resolve()

    gcs_root = Path("/gcs")
    roots = [gcs_root.resolve()] if gcs_root.exists() else [WORKSPACE_ROOT.resolve()]

    projects: list[dict] = []
    seen: set[Path] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            # A manifest sitting directly at a scan root is legacy seed data, not
            # a project — real projects live in their own directory. It must not
            # count as a project dir for pruning either, or a stray root manifest
            # prunes the top-level assets/ tree that new_project writes into.
            is_project_dir = (
                "storyboard_manifest.json" in filenames and here.resolve() != root
            )
            # Prune heavy/irrelevant subtrees. "assets" is only pruned inside a
            # project directory — a top-level /gcs/assets is where new_project
            # puts things, so pruning it unconditionally hid those projects.
            skip = set(IGNORE_DIRS) | {"references", "source"}
            if is_project_dir:
                skip.add("assets")
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]

            if is_project_dir:
                mf = here / "storyboard_manifest.json"
                resolved = mf.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)

                folder_name = mf.parent.name

                name = ""
                channel = "calluses" if "calluses" in str(resolved).lower() else "bestiary"
                beats_count = 0
                script_locked = False
                storyboard_approved = False
                try:
                    manifest_data = json.loads(mf.read_text(encoding="utf-8"))
                    name = (manifest_data.get("title") or "").strip()
                    if "channel" in manifest_data:
                        channel = manifest_data.get("channel") or channel
                    # Read the counts straight off the manifest. Firestore may
                    # override them later, but it is not the only source of
                    # truth — projects that were never bootstrapped into it were
                    # showing "0 beats" despite a full storyboard on disk.
                    beats_count = len(manifest_data.get("shots") or [])
                    script_locked = bool(manifest_data.get("script_locked", False))
                    storyboard_approved = bool(manifest_data.get("storyboard_approved", False))
                except Exception as exc:
                    print(f"Warning: could not read {resolved}: {exc}")
                if not name:
                    name = folder_name

                try:
                    rel_display = resolved.relative_to(root)
                except ValueError:
                    rel_display = Path(mf.name)

                projects.append({
                    "name": name,
                    "rel": str(resolved).replace("\\", "/"),
                    "rel_display": str(rel_display).replace("\\", "/"),
                    "active": resolved == active,
                    "channel": channel,
                    "beats_count": beats_count,
                    "script_locked": script_locked,
                    "storyboard_approved": storyboard_approved,
                })
    projects.sort(key=lambda p: p["name"].lower())
    return projects


def _ref_registry() -> dict:
    if config.REFERENCES_CONFIG.exists():
        return json.loads(config.REFERENCES_CONFIG.read_text(encoding="utf-8"))
    return {}


def _save_ref_registry(reg: dict) -> None:
    config.REFERENCES_CONFIG.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")


def _ref_file(name: str, reg: dict) -> str | None:
    entry = reg.get(name) or {}
    files = entry.get("files") or ([entry["file"]] if entry.get("file") else [])
    return files[0] if files else None


def _suggest_motion_prompt(shot) -> str:
    base = ". ".join(p.strip() for p in (shot.style_medium, shot.prompt) if p and p.strip())
    dur = shot.camera.duration if shot.camera else 6.0
    return (
        f"{base}. Animate this still as the start frame with subtle, restrained in-world "
        f"motion — slow drift, mist/smoke, faint flicker, a gradual reveal; hold the "
        f"composition, no camera cuts. Target length ~{dur:.0f}s."
    ).strip(". ").strip()


def _resolve_local_image_file(path_str: str | None, scene_id: str | None = None) -> Path | None:
    """Resolve a manifest media path. Thin alias over the shared resolver."""
    return config.resolve_media(path_str, scene_id)


def _safe_rel_path(dest: Path) -> str:
    """Manifest-relative path for a file on disk. Thin alias over the shared helper."""
    return config.rel_media_path(dest)


CHAT_MAX_TOKENS = 8000


def claude_chat(system: str, messages: list[dict]) -> str:
    """One conversational turn with Vesper; returns the assistant's text.

    Thinking is on by default on Opus 5 and is billed against the same
    ``max_tokens`` ceiling as the reply, so the budget covers both.
    """
    client = script._client()
    response = client.messages.create(
        model=script.DEFAULT_MODEL,
        max_tokens=CHAT_MAX_TOKENS,
        system=system,
        messages=messages,
        output_config={"effort": "medium"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to respond to this message.")
    return "".join(b.text for b in response.content if b.type == "text")



def resolve_video_model_endpoint(key: str | None) -> str:
    """Map a stored video-model string to a fal endpoint.

    Delegates to the registry in assets.py, whose endpoints are verified against
    fal's OpenAPI listing. The hand-maintained map this replaced sent every Kling
    request to "fal-ai/kling-video/v3/image-to-video" — an endpoint fal has never
    served. It 404s, and it killed a render batch ten minutes in.
    """
    return assets.resolve_video_backend(key)["endpoint"]


def _pad_clip_to_beat(path: Path, target_seconds: float) -> None:
    """Extend a short clip to its beat length by freezing the final frame.

    Paid video models produce a few seconds; a narration-led beat routinely wants
    twenty or more. build_preview concatenates clips raw, so a short clip shortens
    the whole video track — and because the mux uses -shortest, ffmpeg then trims
    the ENTIRE audio mix to match. A 5s clip on a 24s beat silently cut 19 seconds
    of narration off the end of the episode.

    Freezing the last frame keeps the cut honest: the beat holds for as long as
    its voiceover, the generated motion plays out, and the image simply settles.
    """
    try:
        current = timeline._probe_seconds(path)
    except Exception:
        return
    shortfall = float(target_seconds) - float(current)
    if current <= 0 or shortfall < 0.1:
        return

    padded = path.with_name(f"{path.stem}__padded.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path),
             "-vf", f"tpad=stop_mode=clone:stop_duration={shortfall:.3f}",
             "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
             str(padded)],
            check=True,
        )
        padded.replace(path)
        log_job("render", f"  padded to beat length: {current:.1f}s -> {target_seconds:.1f}s (froze final frame)")
    except Exception as exc:  # noqa: BLE001 — an unpadded clip still renders
        log_job("render", f"  !! could not pad clip to beat length: {exc}")
        if padded.exists():
            padded.unlink()


def set_active_video_clip(sb: Storyboard, shot: Shot, video_rel_path: str, out_dir: Path):
    """Promote a generated video variation to be this beat's clip in the cut.

    The source used to be resolved as WORKSPACE_ROOT / video_rel_path — /app on
    Cloud Run, while the file actually lands under /gcs. The path never existed,
    and because the copy sat behind `if src_path.exists()` it was skipped in
    silence: a paid Kling clip was generated, downloaded and billed, then never
    reached render/<scene>.mp4, so the timeline treated the beat as a gap.
    """
    shot.video_clip = video_rel_path
    src_path = config.resolve_media(video_rel_path, shot.scene_id)
    dest_path = out_dir / f"{shot.scene_id}.mp4"
    if src_path is None:
        log_job("render", f"  !! {shot.scene_id}: generated video not found at {video_rel_path} — clip NOT placed in the cut.")
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest_path)
    log_job("render", f"  {shot.scene_id}: clip placed ({dest_path.stat().st_size:,} bytes)")
    if shot.camera:
        _pad_clip_to_beat(dest_path, float(shot.camera.duration))
    try:
        frame_out_path = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
        assets.extract_final_frame(dest_path, frame_out_path)
    except Exception as e:
        print(f"Error extracting final frame for {shot.scene_id}: {e}")


def generate_fal_and_render(sb: Storyboard, force_paid: bool = False) -> None:
    """Render every beat. Local tiers are free and always redone; paid ai_video
    beats that already have a placed clip are kept unless ``force_paid``.

    Without that guard, any re-run — a camera tweak, a fixed FX, a retry after
    one beat failed — silently re-billed every ai_video beat through fal.
    """
    config.require_for("assets")

    out_dir = config.episode_paths(sb.title)["render"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    prev_extracted_frame: Path | None = None
    prev_video_dest_path: Path | None = None
    chaining_mode = getattr(sb.render, "video_chaining", "native_extend")

    # One failing beat must not abandon the rest. A bad fal endpoint used to
    # raise straight out of this loop, so a batch died at beat 6 and the nine
    # remaining beats -- all local and free -- were never rendered at all.
    failures: list[str] = []
    total = len(sb.shots)

    for idx, shot in enumerate(sb.shots, start=1):
        try:
            if getattr(shot, "hero_clip", False):
                log_job("render", f"{shot.scene_id}: Already has imported hero clip - keeping it, not re-rendering.")
                prev_extracted_frame = None
                prev_video_dest_path = None
                continue
            
            # Ensure still image draft is generated
            if not shot.draft_image:
                backend = getattr(shot, "image_model", None) or getattr(sb.render, "backend", None) or "nano2"
                log_job("render", f"Generating drafts for {shot.scene_id} using {backend}...")
                assets.generate_for_shot(shot, n=3, backend=backend, render=sb.render)
                shot.chosen_variation = 0
                shot.draft_image = shot.draft_variations[0]
                save_current_project(sb)
            
            is_ai = (shot.motion_type == MotionType.AI_VIDEO)

            if is_ai and not force_paid and (shot.video_clip or shot.video_variations):
                placed = out_dir / f"{shot.scene_id}.mp4"
                if placed.exists():
                    log_job(
                        "render",
                        f"[{idx}/{total}] {shot.scene_id}: paid clip already rendered — "
                        f"keeping it (re-run with force_paid=true to re-bill).",
                    )
                    prev_extracted_frame = None
                    prev_video_dest_path = placed
                    continue

            if is_ai:
                video_key = getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
                model_endpoint = resolve_video_model_endpoint(video_key)
                log_job("render", f"[{idx}/{total}] PAID video for {shot.scene_id} via {model_endpoint} (chaining: {chaining_mode}) ...")
                target_dur = float(getattr(shot.camera, "duration", 6.0))
                dur_int = max(3, min(10, int(round(target_dur))))

                gen_audio = shot.video_audio
                if gen_audio is None:
                    gen_audio = getattr(sb.render, "video_audio", True)

                motion_prompt = shot.motion_prompt or f"Cinematic motion, high-quality, authentic detail, {shot.prompt}"
                if f"{dur_int}s" not in motion_prompt and "second" not in motion_prompt:
                    motion_prompt = f"{motion_prompt} (duration: ~{dur_int} seconds)"

                arguments = {
                    "prompt": motion_prompt,
                    "duration": str(dur_int),
                    "generate_audio": gen_audio,
                }
                if "veo" in model_endpoint:
                    if dur_int <= 5:
                        arguments["duration"] = "4s"
                    elif dur_int <= 7:
                        arguments["duration"] = "6s"
                    else:
                        arguments["duration"] = "8s"
                elif "seedance" not in model_endpoint:
                    arguments.pop("duration", None)
                    arguments.pop("generate_audio", None)

                # Native Video Extend
                if (chaining_mode == "native_extend" and prev_video_dest_path and prev_video_dest_path.exists()
                        and video_key in ("seedance_2_0", "luma_dream_machine", "hunyuan_video")):
                    log_job("render", f"Native Video Extend: extending from previous segment {prev_video_dest_path.name}...")
                    public_video_url = fal_client.upload_file(str(prev_video_dest_path))
                    arguments["video_url"] = public_video_url
                else:
                    # OpenCV final frame or initial still
                    if chaining_mode != "independent" and prev_extracted_frame and prev_extracted_frame.exists():
                        local_image_path = prev_extracted_frame
                        log_job("render", f"Continuous flow: chaining from final frame -> {local_image_path.name}")
                    else:
                        local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                        if not local_image_path or not local_image_path.exists():
                            log_job("render", f"Still image draft not found for {shot.scene_id}, generating still drafts...")
                            try:
                                assets.generate_for_shot(shot, n=3, backend=sb.render.backend, render=sb.render)
                                shot.chosen_variation = 0
                                shot.draft_image = shot.draft_variations[0]
                                save_current_project(sb)
                                local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                            except Exception as exc:
                                log_job("render", f"  !! Failed to generate still draft for {shot.scene_id}: {exc}")
                                continue

                    if not local_image_path or not local_image_path.exists():
                        log_job("render", f"  !! Still image draft file missing on disk for {shot.scene_id}: {shot.draft_image}")
                        continue
                    
                    log_job("render", f"Uploading starting image {local_image_path.name}...")
                    public_image_url = fal_client.upload_file(str(local_image_path))
                    arguments["image_url"] = public_image_url
            
                log_job("render", f"Triggering fal.ai API with prompt: {motion_prompt[:80]}...")
                result = fal_client.subscribe(model_endpoint, arguments=arguments, with_logs=True)
                video_url = result.get("video", {}).get("url") or result.get("file", {}).get("url")
                if not video_url:
                    raise RuntimeError(f"No video URL returned from fal.ai for {shot.scene_id}")

                import time
                shot_assets_dir = config.ASSETS / shot.scene_id
                shot_assets_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                var_count = len(getattr(shot, "video_variations", []))
                local_video_name = f"video_{timestamp}_{var_count}.mp4"
                local_video_path = shot_assets_dir / local_video_name

                log_job("render", f"Downloading generated video from {video_url} to {local_video_path}...")
                assets._download(video_url, local_video_path)

                video_rel_path = f"assets/{shot.scene_id}/{local_video_name}"
                if not hasattr(shot, "video_variations") or shot.video_variations is None:
                    shot.video_variations = []
                shot.video_variations.append(video_rel_path)

                set_active_video_clip(sb, shot, video_rel_path, out_dir)
                save_current_project(sb)

                dest_video_path = out_dir / f"{shot.scene_id}.mp4"
                prev_video_dest_path = dest_video_path
                log_job("render", f"Successfully generated video for {shot.scene_id}")

                try:
                    frame_out_path = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
                    prev_extracted_frame = assets.extract_final_frame(dest_video_path, frame_out_path)
                except Exception as exc:
                    log_job("render", f"Warning: Failed to extract final frame for continuous chaining on {shot.scene_id}: {exc}")
                    prev_extracted_frame = None
            else:
                log_job("render", f"[{idx}/{total}] Rendering {shot.scene_id} locally ({shot.motion_type.value}) ...")
                motion.render_shot(shot, fps=motion.DEFAULT_FPS, height=motion.DEFAULT_HEIGHT,
                                   out_dir=out_dir, placeholder=False,
                                   motion_cfg=getattr(sb, "motion", None), storyboard=sb)
                prev_extracted_frame = None
                prev_video_dest_path = None
            
        except Exception as exc:  # noqa: BLE001 -- resilient batch
            failures.append(shot.scene_id)
            log_job("render", f"  !! {shot.scene_id} FAILED: {exc.__class__.__name__}: {exc}")
            prev_extracted_frame = None
            prev_video_dest_path = None

    if failures:
        log_job("render", f"Finished with {len(failures)} failed beat(s): {failures} -- re-run to retry just these.")
    else:
        log_job("render", f"All {total} beat(s) rendered.")


def ensure_gcs_projects():
    gcs_root = Path("/gcs").resolve()
    if not gcs_root.exists():
        gcs_root = WORKSPACE_ROOT
        
    # 1. Setup isolated directories
    manananggal_dir = gcs_root / "bestiary" / "manananggal"
    manananggal_dir.mkdir(parents=True, exist_ok=True)
    manananggal_manifest = manananggal_dir / "storyboard_manifest.json"
    
    leshy_dir = gcs_root / "bestiary" / "leshy"
    leshy_dir.mkdir(parents=True, exist_ok=True)
    leshy_manifest = leshy_dir / "storyboard_manifest.json"
    
    # Copy Manananggal if missing or empty
    local_bestiary = Path(WORKSPACE_ROOT) / "storyboard_manifest.bestiary.json"
    if local_bestiary.exists():
        should_copy = True
        if manananggal_manifest.exists():
            try:
                data = json.loads(manananggal_manifest.read_text(encoding="utf-8"))
                if len(data.get("shots", [])) > 0:
                    should_copy = False
            except Exception:
                pass
        if should_copy:
            try:
                data = json.loads(local_bestiary.read_text(encoding="utf-8"))
                data["channel"] = "bestiary"
                manananggal_manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
                print(f"Copied default manananggal manifest to {manananggal_manifest}")
            except Exception as e:
                print(f"Failed to copy manananggal manifest: {e}")
            
    # Copy Leshy if missing or empty
    local_calluses = Path(WORKSPACE_ROOT) / "storyboard_manifest.calluses.json"
    if local_calluses.exists():
        should_copy = True
        if leshy_manifest.exists():
            try:
                data = json.loads(leshy_manifest.read_text(encoding="utf-8"))
                if len(data.get("shots", [])) > 0:
                    should_copy = False
            except Exception:
                pass
        if should_copy:
            try:
                data = json.loads(local_calluses.read_text(encoding="utf-8"))
                data["channel"] = "bestiary" # Force Leshy onto bestiary channel!
                leshy_manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
                print(f"Copied default leshy manifest to {leshy_manifest}")
            except Exception as e:
                print(f"Failed to copy leshy manifest: {e}")

    # NOTE: this seeds *missing* project manifests only. It must never delete or
    # rewrite existing ones — this runs on every cold start, and Cloud Run scales
    # to zero, so anything destructive here silently eats user data between
    # sessions. (An earlier version unlinked six manifest paths, rmtree'd
    # calluses/, and nulled every draft_image whose filename contained "var_" —
    # which is every image assets.py generates.)


# --- API ENDPOINTS ---

@app.get("/api/projects")
def get_projects(channel: Optional[str] = None):
    try:
        scanned = _scan_projects()
        # Fetch actual statistics and names from Firestore
        try:
            db_projects = manifest.list_projects(channel)
            db_map = {p["id"]: p for p in db_projects}
        except Exception as fe:
            print(f"Warning: Firestore list_projects failed: {fe}")
            db_map = {}
        
        res = []
        for p in scanned:
            # _scan_projects already filled these from the manifest on disk;
            # Firestore refines them only where it actually has a value, so an
            # unreachable or un-bootstrapped Firestore no longer zeroes the UI.
            f_id = get_project_id_from_path(p["rel"])
            doc = db_map.get(f_id)
            if doc:
                p["name"] = doc.get("title") or p["name"]
                p["channel"] = doc.get("channel") or p["channel"]
                if doc.get("beats_count"):
                    p["beats_count"] = doc["beats_count"]
                p["script_locked"] = doc.get("script_locked", p["script_locked"])
                p["storyboard_approved"] = doc.get("storyboard_approved", p["storyboard_approved"])
            res.append(p)
            
        if channel:
            res = [p for p in res if p["channel"] == channel]
        return {"ok": True, "projects": res}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


def _resolved_video_key(raw: str | None) -> str:
    """Registry key a stored video_model actually resolves to.

    resolve_video_backend() returns the entry, not its key, and the UI needs the
    key to select the right option.
    """
    entry = assets.resolve_video_backend(raw)
    for k, v in assets.VIDEO_BACKENDS.items():
        if v is entry:
            return k
    return "seedance_2_0"


@app.get("/api/project/active")
def get_active_project():
    try:
        sb = get_current_project()
        reg = _ref_registry()

        # Everything below is served off the GCS FUSE mount, where a stat is a
        # network round trip. episode_paths() used to be recomputed inside the
        # shot loop and each beat cost its own .exists(); asking for narration
        # and sfx the same way would have tripled that. List each directory once
        # and test membership instead.
        ep = config.episode_paths(sb.title)

        def _stems(d) -> set[str]:
            try:
                return {p.stem for p in d.iterdir() if p.is_file()}
            except (OSError, FileNotFoundError):
                return set()

        rendered_stems = _stems(ep["render"])
        narration_stems = _stems(ep["narration"])
        sfx_stems = _stems(ep["sfx"])

        # Prepare metadata structure
        shots_payload = []
        for s in sb.shots:
            s_dict = asdict(s)
            s_dict["motion_type"] = s.motion_type.value
            s_dict["references_resolved"] = [
                {"name": n, "file": _ref_file(n, reg)} for n in s.references
            ]
            s_dict["motion_prompt_suggestion"] = _suggest_motion_prompt(s)
            # Paths are relative to a media root; the frontend prefixes /media/
            # exactly once. episode_paths now lives inside the project directory,
            # so there is no slug segment.
            has_clip = s.scene_id in rendered_stems
            s_dict["active_clip_url"] = (
                config.rel_media_path(ep["render"] / f"{s.scene_id}.mp4") if has_clip else None
            )
            # Step gating needs to know which stages a beat has actually been
            # through -- the workflow header cannot gate Audio Studio or Editing
            # on state nobody reports.
            s_dict["has_narration"] = s.scene_id in narration_stems
            s_dict["has_sfx"] = s.scene_id in sfx_stems
            # Media-root-relative; the frontend prefixes /media/ exactly once.
            s_dict["narration_url"] = (
                config.rel_media_path(ep["narration"] / f"{s.scene_id}.mp3")
                if s_dict["has_narration"] else None
            )
            s_dict["sfx_url"] = (
                config.rel_media_path(ep["sfx"] / f"{s.scene_id}.mp3")
                if s_dict["has_sfx"] else None
            )
            # Manifests still carry legacy values -- raw endpoint strings and
            # keys that were never in the registry (e.g. the dead
            # "fal-ai/kling-video/v3/image-to-video"). The resolver aliases them
            # at render time, but a <select> bound to a value absent from its
            # options renders blank and can rewrite the field on the next touch.
            # Report the key that will actually be used so the UI shows the truth.
            if s.motion_type == MotionType.AI_VIDEO:
                raw = s.video_model or sb.render.video_model
                s_dict["video_model_key"] = _resolved_video_key(raw)
                s_dict["video_model_is_legacy"] = s_dict["video_model_key"] != (s.video_model or "")
            else:
                s_dict["video_model_key"] = None
                s_dict["video_model_is_legacy"] = False
            shots_payload.append(s_dict)

        counts = {
            "beats": len(sb.shots),
            "stills": sum(1 for s in sb.shots if s.draft_image),
            "narration": sum(1 for s in sb.shots if s.scene_id in narration_stems),
            "sfx": sum(1 for s in sb.shots if s.scene_id in sfx_stems),
            "rendered": sum(1 for s in sb.shots if s.scene_id in rendered_stems),
        }

        # "_preview" is already in the render listing above -- no extra stat.
        preview_file = ep["render"] / "_preview.mp4"
        preview_url = config.rel_media_path(preview_file) if "_preview" in rendered_stems else None
        
        # Same location timeline.build writes to: the project directory.
        fcpxml_file = config.MANIFEST_PATH.parent / f"{ep['slug']}.fcpxml"
        fcpxml_ready = fcpxml_file.exists()
        
        # Count paid video shots
        paid_count = len(sb.paid_shots())
        
        # Options map to pass to frontend
        # Derived from the registry in assets.py, so the dropdown, the script
        # stage's enum and the implemented backends cannot drift apart again.
        image_backends = {k: v["label"] for k, v in assets.IMAGE_BACKENDS.items()}


        # Same registry the resolver uses, for the same reason image_backends
        # does it. This dict used to be written out by hand and had drifted:
        # it offered "kling_2_5_turbo_pro", which is not a registry key, so
        # resolve_video_backend() fell through to a substring match and billed
        # Kling 2.1 Standard instead. Picking a model must render that model.
        video_backends = {k: v["label"] for k, v in assets.VIDEO_BACKENDS.items()}
        
        return {
            "ok": True,
            "project": {
                "id": sb.id,
                "title": sb.title,
                "channel": sb.channel,
                "cultural_origin": sb.cultural_origin,
                "script_locked": sb.script_locked,
                "storyboard_approved": sb.storyboard_approved,
                "voice_id": getattr(sb, "voice_id", "") or "",
                "music_track": sb.music_track or "",
                "render": asdict(sb.render),
                "shots": shots_payload,
            },
            # Shipped alongside the project so the mix and motion panels do not
            # each need their own round trip on every step change.
            "mix": asdict(sb.mix),
            "grade": asdict(sb.grade),
            "motion": asdict(sb.motion),
            "counts": counts,
            "preview_url": preview_url,
            "fcpxml_ready": fcpxml_ready,
            "ep_slug": ep["slug"],
            "paid_count": paid_count,
            "image_backends": image_backends,
            "video_backends": video_backends,
            "tiers": {
                "static": "Tier A: Still + procedural FX ($0)",
                "parallax": "Tier B: 2.5D parallax shift ($0)",
                "ai_video": "Tier C: Paid fal video generation",
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/project/select")
async def select_project(request: Request):
    try:
        data = await request.json()
        rel_path = data.get("rel")
        if not rel_path:
            raise HTTPException(status_code=400, detail="Missing relative path")

        p = Path(rel_path).resolve()
        # Only a manifest inside a known workspace root may be activated —
        # otherwise this endpoint points the studio at arbitrary paths on disk.
        roots = [WORKSPACE_ROOT.resolve()]
        gcs_root = Path("/gcs")
        if gcs_root.exists():
            roots.append(gcs_root.resolve())
        if not any(root in p.parents for root in roots):
            raise HTTPException(status_code=400, detail="Project path is outside the workspace")
        if p.name != "storyboard_manifest.json" or not p.is_file():
            raise HTTPException(status_code=404, detail="Project file not found on disk")

        set_active_manifest_path(str(p))
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/project/meta")
async def update_project_meta(request: Request):
    try:
        sb = get_current_project()
        data = await request.json()
        
        if "title" in data:
            sb.title = str(data["title"]).strip()
        if "channel" in data:
            sb.channel = str(data["channel"]).strip()
            
        save_current_project(sb)
        return {"ok": True, "title": sb.title, "channel": sb.channel}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/project/new")
async def new_project(request: Request):
    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        channel = (data.get("channel") or "bestiary").strip()
        
        # Projects live at <root>/<channel>/<name>/, matching where
        # ensure_gcs_projects seeds them. They used to be written under
        # <root>/assets/<channel>/<name>/ instead, which is both a second
        # convention for the same thing and a directory the project scan prunes
        # -- so every project created here was invisible in the sidebar while
        # still being set as the active project.
        root = Path("/gcs") if Path("/gcs").exists() else WORKSPACE_ROOT
        channel_dir = root / channel
        channel_dir.mkdir(parents=True, exist_ok=True)

        name = secure_filename(name) if name else ""
        if not name:
            n = 1
            while (channel_dir / f"project_{n}").exists():
                n += 1
            name = f"project_{n}"

        proj_dir = channel_dir / name
        proj_dir.mkdir(parents=True, exist_ok=True)
        
        manifest_file = proj_dir / "storyboard_manifest.json"
        
        sb = Storyboard(title=name, channel=channel)
        manifest.save(sb, manifest_file)
        
        # Save to Firestore
        f_id = get_project_id_from_path(manifest_file)
        sb.id = f_id
        try:
            manifest.save_project(sb)
        except Exception as fe:
            print(f"Warning: Firestore save_project failed: {fe}")
        
        set_active_manifest_path(manifest_file)
        config.set_active_manifest(manifest_file)
        
        return {"ok": True, "rel": str(manifest_file.resolve()).replace("\\", "/")}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/render")
async def update_render_knobs(request: Request):
    try:
        sb = get_current_project()
        data = await request.json()
        r = sb.render
        
        if "backend" in data:
            r.backend = str(data["backend"])
        if "video_model" in data:
            r.video_model = str(data["video_model"])
        if "video_chaining" in data:
            r.video_chaining = str(data["video_chaining"])
        if "video_audio" in data:
            r.video_audio = bool(data["video_audio"])
        if "guidance_scale" in data:
            r.guidance_scale = float(data["guidance_scale"])
        if "nag_scale" in data:
            r.nag_scale = float(data["nag_scale"])
        if "num_inference_steps" in data:
            r.num_inference_steps = int(data["num_inference_steps"])
        if "negative_prompt" in data:
            r.negative_prompt = str(data["negative_prompt"])
            
        save_current_project(sb)
        return {"ok": True, "render": asdict(r)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/motion")
def get_motion():
    """Project parallax defaults, plus the resolved travel for every beat.

    `resolved` is what will actually render, so the UI can show the effective
    number next to a beat instead of making the user infer it from the rate.
    """
    sb = get_current_project()
    cfg = sb.motion
    beats = []
    for s in sb.shots:
        cam = s.camera
        amount = float(getattr(cam, "amount", 0.0) or 0.0)
        z, p = motion.camera_amounts(cam.duration, amount, cam.speed, cfg)
        # A beat only moves if it is the parallax tier AND has a real move.
        # Reporting travel for a static plate or a Tier-C clip would show the UI
        # a number that never renders.
        if s.motion_type != MotionType.PARALLAX or cam.move not in ("push_in", "push_out", "pan_left", "pan_right"):
            travel = 0.0
        else:
            travel = z if cam.move in ("push_in", "push_out") else p
        beats.append({
            "scene_id": s.scene_id,
            "motion_type": s.motion_type.value,
            "move": cam.move,
            "duration": round(float(cam.duration), 2),
            "duration_locked": bool(getattr(cam, "duration_locked", False)),
            "speed": round(float(cam.speed), 3),
            "amount": round(amount, 4),           # 0 = inherit the project rate
            "travel": round(travel, 4),           # total, e.g. 0.15 -> 115% end scale
            "rate_pct_per_sec": round(travel / max(0.1, float(cam.duration)) * 100, 3),
        })
    return {"ok": True, "motion": asdict(cfg), "beats": beats}


@app.post("/api/motion")
async def update_motion(request: Request):
    """Set the project's parallax defaults. Re-render to see them."""
    try:
        sb = get_current_project()
        data = await request.json()
        limits = {"speed": (0.0, 6.0), "zoom_rate": (0.0, 0.10), "pan_rate": (0.0, 0.10),
                  "zoom_max": (0.0, 0.80), "pan_max": (0.0, 0.80)}
        for key, (lo, hi) in limits.items():
            if key in data and data[key] is not None:
                setattr(sb.motion, key, max(lo, min(hi, float(data[key]))))
        save_current_project(sb)
        return {"ok": True, "motion": asdict(sb.motion)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/motion/preview/{scene_id}")
def preview_motion(scene_id: str):
    """Re-render one beat with the current settings, nothing else.

    Tuning parallax by re-rendering the whole episode is a ~25 minute loop for a
    number you may change again immediately. This renders the single beat you
    are looking at.
    """
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        if shot.motion_type == MotionType.AI_VIDEO:
            raise HTTPException(status_code=400,
                                detail="Beat is Tier-C ai_video; parallax settings do not apply.")
        out_dir = config.episode_paths(sb.title)["render"]

        def fn():
            p = motion.render_shot(shot, fps=motion.DEFAULT_FPS, height=motion.DEFAULT_HEIGHT,
                                   out_dir=out_dir, placeholder=False,
                                   motion_cfg=getattr(sb, "motion", None), storyboard=sb)
            z, pan = motion.camera_amounts(
                shot.camera.duration, float(getattr(shot.camera, "amount", 0.0) or 0.0),
                shot.camera.speed, sb.motion)
            travel = z if shot.camera.move in ("push_in", "push_out") else pan
            log_job("motion_preview",
                    f"{scene_id}: {shot.camera.move} {travel*100:.1f}% over "
                    f"{shot.camera.duration:.1f}s ({travel/max(0.1, shot.camera.duration)*100:.2f} %/s) "
                    f"-> {_safe_rel_path(p)}")

        start_job("motion_preview", fn)
        return {"ok": True, "scene_id": scene_id}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/grade")
def get_grade():
    """Episode look, plus each beat's resolved grade and sparse override."""
    sb = get_current_project()
    return {
        "ok": True,
        "grade": asdict(sb.grade),
        "channel": sb.channel,
        "beats": [
            {"scene_id": s.scene_id,
             "override": dict(getattr(s, "grade", None) or {}),
             "resolved": motion.resolve_grade(sb, s)}
            for s in sb.shots
        ],
    }


@app.post("/api/grade")
async def update_grade(request: Request):
    """Set the episode look. Re-render to see it."""
    try:
        sb = get_current_project()
        data = await request.json()
        limits = {"brightness": (-3.0, 3.0), "contrast": (-1.0, 1.0),
                  "temperature": (2000, 12000), "saturation": (0.0, 2.0),
                  "rim_light": (0.0, 1.0), "key_intensity": (0.0, 1.0)}
        for key, (lo, hi) in limits.items():
            if key in data and data[key] is not None:
                v = max(lo, min(hi, float(data[key])))
                setattr(sb.grade, key, int(v) if key == "temperature" else v)
        if "key_light" in data:
            kl = str(data["key_light"] or "").strip().lower()
            if kl and kl not in motion._KEY_DIRS:
                raise HTTPException(status_code=400, detail=f"Unknown key_light: {kl}")
            sb.grade.key_light = kl
        save_current_project(sb)
        return {"ok": True, "grade": asdict(sb.grade)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/mix")
def get_mix():
    """Audio mix levels for the active episode (linear gain, 1.0 = unity)."""
    sb = get_current_project()
    return {"ok": True, "mix": asdict(sb.mix)}


@app.post("/api/mix")
async def update_mix(request: Request):
    """Set narration / sfx / music levels. Re-run the preview to hear them."""
    try:
        sb = get_current_project()
        data = await request.json()
        for key in ("narration", "sfx", "music"):
            if key in data and data[key] is not None:
                setattr(sb.mix, key, max(0.0, min(2.0, float(data[key]))))
        save_current_project(sb)
        return {"ok": True, "mix": asdict(sb.mix)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/render/reference")
async def set_global_reference(file: UploadFile = File(...)):
    try:
        sb = get_current_project()
        config.require_for("assets")
        
        config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        fname = secure_filename(f"global_ref_{file.filename}")
        dest = config.REFERENCES_DIR / fname
        
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        public_url = fal_client.upload_file(str(dest))
        sb.render.reference_image = f"references/{fname}"
        sb.render.reference_image_url = public_url
        
        save_current_project(sb)
        return {"ok": True, "reference_image": sb.render.reference_image}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


MUSIC_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


@app.get("/api/music")
def list_music():
    """Music beds available to any project.

    The pool is shared rather than per-project — a bed is reusable across
    episodes — and lives on the GCS mount. It used to resolve to ROOT/audio_pool,
    i.e. /app, so an uploaded track died with the container and every timeline
    silently lost its music on the next cold start.
    """
    try:
        pool = config.AUDIO_POOL
        pool.mkdir(parents=True, exist_ok=True)
        tracks = []
        for f in sorted(pool.iterdir()):
            if f.is_file() and f.suffix.lower() in MUSIC_SUFFIXES:
                tracks.append({
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "url": config.rel_media_path(f),
                })
        sb = get_current_project()
        return {"ok": True, "tracks": tracks, "selected": sb.music_track or ""}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/music")
async def upload_music(file: UploadFile = File(...)):
    """Add a track to the shared pool. Curated/licensed audio only (see CLAUDE.md)."""
    try:
        name = secure_filename(file.filename or "track.mp3")
        if Path(name).suffix.lower() not in MUSIC_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio type. Use one of: {', '.join(sorted(MUSIC_SUFFIXES))}",
            )
        config.AUDIO_POOL.mkdir(parents=True, exist_ok=True)
        dest = config.AUDIO_POOL / name
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return {"ok": True, "name": name, "url": config.rel_media_path(dest),
                "size_bytes": dest.stat().st_size}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/music/generate")
async def generate_music_endpoint(request: Request):
    """Generate a music bed into the shared pool. Paid (fal.ai).

    Defaults to the episode's own music_prompt written by the script stage, and
    to roughly the episode runtime — clamped per model, since the timeline loops
    the bed anyway. Runs in the background: the longer models take minutes.
    """
    try:
        config.require_for("assets")
        sb = get_current_project()
        data = await request.json()

        prompt = (data.get("prompt") or getattr(sb, "music_prompt", "") or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail="No music prompt. Pass one, or redraft the script so Vesper writes music_prompt.",
            )
        backend = (data.get("backend") or audio.DEFAULT_MUSIC_BACKEND).strip()
        if backend not in audio.MUSIC_BACKENDS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown music backend. Choose from: {', '.join(audio.MUSIC_BACKEND_KEYS)}",
            )
        runtime = sum(float(s.camera.duration) for s in sb.shots if s.camera) or 180.0
        seconds = float(data.get("duration_seconds") or runtime)
        select = bool(data.get("select", True))
        name = secure_filename(data.get("name") or f"{config.episode_paths(sb.title)['slug'][:40]}_bed.mp3")
        if Path(name).suffix.lower() not in MUSIC_SUFFIXES:
            name += ".mp3"

        def fn():
            dest = audio.generate_music(
                prompt, config.AUDIO_POOL / name, duration_seconds=seconds,
                backend=backend, log=lambda m: log_job("music", m),
            )
            if select:
                current = get_current_project()
                current.music_track = dest.name
                save_current_project(current)
                log_job("music", f"Selected {dest.name} as this episode's bed.")

        if start_job("music", fn):
            return {"ok": True, "stage": "music", "name": name, "backend": backend,
                    "duration_seconds": seconds}
        return JSONResponse(status_code=409, content={"ok": False, "error": "music generation already running"})
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/music/select")
async def select_music(request: Request):
    """Set (or clear, with an empty name) this episode's music bed."""
    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        if name:
            if Path(name).name != name:
                raise HTTPException(status_code=400, detail="Track name must not contain a path")
            if not (config.AUDIO_POOL / name).is_file():
                raise HTTPException(status_code=404, detail=f"No such track in the pool: {name}")
        sb = get_current_project()
        sb.music_track = name or None
        save_current_project(sb)
        return {"ok": True, "music_track": sb.music_track or ""}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/render/reference/clear")
def clear_global_reference():
    try:
        sb = get_current_project()
        sb.render.reference_image = ""
        sb.render.reference_image_url = ""
        save_current_project(sb)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}")
async def update_shot(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        data = await request.json()
        
        if "chosen_variation" in data:
            idx = data["chosen_variation"]
            shot.chosen_variation = idx
            if idx is not None and 0 <= idx < len(shot.draft_variations):
                shot.draft_image = shot.draft_variations[idx]
        if "chosen_video_variation" in data:
            idx = data["chosen_video_variation"]
            if idx is not None and 0 <= idx < len(getattr(shot, "video_variations", [])):
                set_active_video_clip(sb, shot, shot.video_variations[idx], config.episode_paths(sb.title)["render"])
        if "motion_type" in data:
            shot.motion_type = MotionType(data["motion_type"])
            if shot.motion_type == MotionType.AI_VIDEO:
                shot.video_model = shot.video_model or getattr(sb.render, "video_model", "seedance_2_0")
            else:
                shot.video_model = None
        if "video_model" in data:
            shot.video_model = str(data["video_model"])
        if "video_audio" in data:
            shot.video_audio = None if data["video_audio"] is None else bool(data["video_audio"])
        if "narration" in data:
            shot.narration = data["narration"]
        if "prompt" in data:
            shot.prompt = data["prompt"]
        if "style_medium" in data:
            shot.style_medium = data["style_medium"]
        if "motion_prompt" in data:
            shot.motion_prompt = data["motion_prompt"]
        if "flow_hero" in data:
            shot.flow_hero = bool(data["flow_hero"])
        if "sfx" in data:
            shot.sfx = str(data["sfx"] or "")
        for k in ("gain_narration", "gain_sfx"):
            if k in data and data[k] is not None:
                # 0..4 linear (-inf..+12 dB). Trims sit on top of the episode bus.
                setattr(shot, k, max(0.0, min(4.0, float(data[k]))))
        if "grade" in data:
            # Sparse: only the keys present differ from the episode grade. Sending
            # null for a key clears the override rather than pinning a value.
            g = data["grade"]
            if g is None:
                shot.grade = {}
            elif isinstance(g, dict):
                allowed = set(asdict(sb.grade))
                for k, v in g.items():
                    if k not in allowed:
                        continue
                    if v is None:
                        shot.grade.pop(k, None)
                    else:
                        shot.grade[k] = v
        if "camera" in data and isinstance(data["camera"], dict):
            # FlowCanvas has been PATCHing `camera` all along; without this the
            # write was accepted and silently discarded, so no camera edit ever
            # reached the renderer.
            cam = data["camera"]
            if "move" in cam:
                move = str(cam["move"] or "static")
                if move not in CAMERA_MOVES:
                    raise HTTPException(status_code=400, detail=f"Unknown camera move: {move}")
                shot.camera.move = move
            if "speed" in cam and cam["speed"] is not None:
                shot.camera.speed = max(0.1, min(4.0, float(cam["speed"])))
            if "amount" in cam and cam["amount"] is not None:
                # 0 = auto (derive from beat duration); otherwise total travel,
                # e.g. 0.15 == a 100% -> 115% push across the beat.
                shot.camera.amount = max(0.0, min(0.60, float(cam["amount"])))
            if "duration" in cam and cam["duration"] is not None:
                shot.camera.duration = max(0.2, float(cam["duration"]))
            if "duration_locked" in cam and cam["duration_locked"] is not None:
                shot.camera.duration_locked = bool(cam["duration_locked"])

        save_current_project(sb)
        return {"ok": True, "paid_count": len(sb.paid_shots()), "camera": asdict(shot.camera)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/reference")
async def add_shot_reference(scene_id: str, file: UploadFile = File(...)):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        fname = secure_filename(f"{scene_id}_{file.filename}")
        config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.REFERENCES_DIR / fname
        
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        name = dest.stem
        reg = _ref_registry()
        reg[name] = {"files": [fname]}
        _save_ref_registry(reg)
        
        if name not in shot.references:
            shot.references.append(name)
            
        save_current_project(sb)
        return {"ok": True, "name": name, "file": fname, "references": shot.references}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/reference/remove")
async def remove_shot_reference(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        data = await request.json()
        name = (data.get("name") or "").strip()
        
        if name not in shot.references:
            raise HTTPException(status_code=400, detail="Reference not associated with this shot")
            
        shot.references.remove(name)
        save_current_project(sb)
        return {"ok": True, "references": shot.references}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/image")
async def upload_shot_image(scene_id: str, file: UploadFile = File(...)):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        dest_dir = config.ASSETS / scene_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        base = secure_filename(file.filename) or "image.png"
        stem, _, ext = base.rpartition(".")
        stem, ext = (stem or base), (ext or "png")
        
        n = 0
        while (dest_dir / f"upload_{n}_{stem}.{ext}").exists():
            n += 1
        dest = dest_dir / f"upload_{n}_{stem}.{ext}"
        
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        rel = _safe_rel_path(dest)
        shot.draft_variations.append(rel)
        shot.chosen_variation = len(shot.draft_variations) - 1
        shot.draft_image = rel
        
        save_current_project(sb)
        return {"ok": True, "path": rel, "chosen": shot.chosen_variation, "variations": len(shot.draft_variations)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/edit_image/{var_idx}")
async def edit_shot_image(scene_id: str, var_idx: int, request: Request):
    try:
        config.require_for("image")
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot or not shot.draft_variations:
            raise HTTPException(status_code=404, detail="Scene or draft variations not found")

        if var_idx < 0 or var_idx >= len(shot.draft_variations):
            raise HTTPException(status_code=400, detail="Index out of range")

        data = await request.json()
        edit_prompt = (data.get("prompt") or "").strip()
        if not edit_prompt:
            raise HTTPException(status_code=400, detail="Empty edit prompt")

        backend = (data.get("backend") or getattr(sb.render, "backend", None) or assets.DEFAULT_BACKEND)

        # 1. Resolve local file path
        rel_path = shot.draft_variations[var_idx]
        local_path = _resolve_local_image_file(rel_path, scene_id=scene_id)
        if not local_path or not local_path.exists():
            raise HTTPException(status_code=400, detail=f"Base image variation not found on disk: {rel_path}")

        # 2. Upload the starting image to Fal
        print(f"Uploading base image {local_path.name} to Fal...")
        public_image_url = fal_client.upload_file(str(local_path))

        # 3. Call the assets editor
        print(f"Calling edit on Fal using backend {backend} with prompt: {edit_prompt}...")
        gen_urls = assets.generate_image_edit(
            public_image_url=public_image_url,
            prompt=edit_prompt,
            n=3,
            backend=backend,
            render=sb.render
        )

        # 4. Download and save the new variations
        import time
        ts = int(time.time())
        rel_paths = []
        for i, url in enumerate(gen_urls):
            dest = config.ASSETS / scene_id / f"edit_{ts}_{i}.png"
            assets._download(url, dest)
            rel = _safe_rel_path(dest)
            rel_paths.append(rel)

        # 5. Append new variations to the shot manifest
        existing = list(shot.draft_variations or [])
        new_start_idx = len(existing)
        shot.draft_variations = existing + rel_paths
        if rel_paths:
            shot.chosen_variation = new_start_idx
            shot.draft_image = rel_paths[0]

        save_current_project(sb)
        return {"ok": True, "variations": shot.draft_variations, "chosen": shot.chosen_variation}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/clip")
async def upload_shot_clip(scene_id: str, file: UploadFile = File(...)):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        ep = config.episode_paths(sb.title)
        ep["render"].mkdir(parents=True, exist_ok=True)
        
        tmp_dir = Path(tempfile.gettempdir())
        tmp = tmp_dir / secure_filename(f"heroin_{scene_id}_{file.filename}")
        
        with open(tmp, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        dest = ep["render"] / f"{scene_id}.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(tmp),
                 "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                        "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                 "-r", "24", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-an", str(dest)],
                check=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"FFmpeg normalization failed: {exc}")
        finally:
            if tmp.exists():
                tmp.unlink()
                
        dur = timeline._probe_seconds(dest)
        if shot.camera and dur > 0:
            shot.camera.duration = round(dur, 2)
        shot.hero_clip = True
        
        save_current_project(sb)
        return {"ok": True, "duration": round(dur, 2), "path": _safe_rel_path(dest)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/generate_video")
async def generate_shot_video(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        config.require_for("video")
        data = await request.json()
        video_model_key = data.get("video_model") or getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
        shot.video_model = video_model_key
        shot.motion_type = MotionType.AI_VIDEO

        model_endpoint = resolve_video_model_endpoint(video_model_key)
        
        local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
        if not local_image_path or not local_image_path.exists():
            print("Auto-generating still drafts before video render...")
            assets.generate_for_shot(shot, n=3, backend=sb.render.backend, render=sb.render)
            shot.chosen_variation = 0
            shot.draft_image = shot.draft_variations[0]
            save_current_project(sb)
            local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)

        if not local_image_path or not local_image_path.exists():
            raise HTTPException(status_code=400, detail="Missing starting still image draft")

        out_dir = config.episode_paths(sb.title)["render"]
        out_dir.mkdir(parents=True, exist_ok=True)

        public_image_url = fal_client.upload_file(str(local_image_path))
        target_dur = float(getattr(shot.camera, "duration", 6.0))
        dur_int = max(3, min(10, int(round(target_dur))))

        motion_prompt = shot.motion_prompt or f"Cinematic motion, high-quality, authentic detail, {shot.prompt}"
        if f"{dur_int}s" not in motion_prompt and "second" not in motion_prompt:
            motion_prompt = f"{motion_prompt} (duration: ~{dur_int} seconds)"

        gen_audio = shot.video_audio
        if gen_audio is None:
            gen_audio = getattr(sb.render, "video_audio", True)

        arguments = {
            "image_url": public_image_url,
            "prompt": motion_prompt,
            "duration": str(dur_int),
            "generate_audio": gen_audio,
        }
        if "veo" in model_endpoint:
            if dur_int <= 5:
                arguments["duration"] = "4s"
            elif dur_int <= 7:
                arguments["duration"] = "6s"
            else:
                arguments["duration"] = "8s"
        elif "seedance" not in model_endpoint:
            arguments.pop("duration", None)
            arguments.pop("generate_audio", None)

        # Extend previous segment natively
        chaining_mode = getattr(sb.render, "video_chaining", "native_extend")
        shot_idx = next((i for i, s in enumerate(sb.shots) if s.scene_id == scene_id), 0)
        if shot_idx > 0:
            prev_shot = sb.shots[shot_idx - 1]
            prev_video_path = out_dir / f"{prev_shot.scene_id}.mp4"
            if chaining_mode == "native_extend" and prev_video_path.exists() and video_model_key in ("seedance_2_0", "luma_dream_machine", "hunyuan_video"):
                public_video_url = fal_client.upload_file(str(prev_video_path))
                arguments["video_url"] = public_video_url

        result = fal_client.subscribe(model_endpoint, arguments=arguments, with_logs=True)
        video_url = result.get("video", {}).get("url") or result.get("file", {}).get("url")
        if not video_url:
            raise RuntimeError("No video URL returned from fal.ai")

        import time
        shot_assets_dir = config.ASSETS / scene_id
        shot_assets_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        var_count = len(getattr(shot, "video_variations", []))
        local_video_name = f"video_{timestamp}_{var_count}.mp4"
        local_video_path = shot_assets_dir / local_video_name

        assets._download(video_url, local_video_path)

        video_rel_path = f"assets/{scene_id}/{local_video_name}"
        if not hasattr(shot, "video_variations") or shot.video_variations is None:
            shot.video_variations = []
        shot.video_variations.append(video_rel_path)

        set_active_video_clip(sb, shot, video_rel_path, out_dir)
        save_current_project(sb)

        return {"ok": True, "video_path": f"/render/{scene_id}.mp4", "video_model": video_model_key}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/regenerate/{scene_id}")
async def regenerate(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        data = await request.json()
        # Explicit request wins, then the beat's own override, then the episode
        # default. The middle term was missing, so regenerating a beat ignored
        # the model Claude picked for it.
        backend = (
            data.get("backend")
            or getattr(shot, "image_model", None)
            or getattr(sb.render, "backend", None)
            or assets.DEFAULT_BACKEND
        )

        assets.generate_for_shot(shot, n=3, backend=backend, render=sb.render)
        save_current_project(sb)
        return {"ok": True, "variations": shot.draft_variations, "backend": backend}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/chat")
async def shot_chat(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        config.require_for("script")
        data = await request.json()
        messages = data.get("messages", [])
        
        system_prompt = (
            f"You are Vesper. Converse with the user to refine the documentary narration "
            f"and visual descriptions for storyboard beat {scene_id} (topic context: {sb.title}).\n\n"
            f"Current Narration:\n\"{shot.narration}\"\n\n"
            f"Current Visual Prompt:\n\"{shot.prompt}\"\n\n"
            f"Current Style Medium:\n\"{shot.style_medium}\"\n\n"
            f"Current Motion Prompt:\n\"{shot.motion_prompt}\"\n\n"
            f"If you proposed a refinement, return a JSON block wrapping the updated prompts in fields:\n"
            f"\"refined_prompt\" and/or \"refined_motion_prompt\"."
        )
        
        reply = claude_chat(system_prompt, messages)

        # Look for refined prompts in JSON format inside the response
        refined_prompt = None
        refined_motion_prompt = None
        try:
            match = re.search(r"({.*})", reply, re.DOTALL)
            if match:
                j = json.loads(match.group(1).strip())
                refined_prompt = j.get("refined_prompt")
                refined_motion_prompt = j.get("refined_motion_prompt")
        except Exception:
            pass
            
        return {
            "ok": True, 
            "reply": reply, 
            "refined_prompt": refined_prompt, 
            "refined_motion_prompt": refined_motion_prompt
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/apply_chat_prompts")
async def apply_chat_prompts(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        data = await request.json()
        if "refined_prompt" in data and data["refined_prompt"]:
            shot.prompt = str(data["refined_prompt"])
        if "refined_motion_prompt" in data and data["refined_motion_prompt"]:
            shot.motion_prompt = str(data["refined_motion_prompt"])
            
        save_current_project(sb)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/delete_image/{var_idx}")
def delete_image_variation(scene_id: str, var_idx: int):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        if not shot.draft_variations or var_idx < 0 or var_idx >= len(shot.draft_variations):
            raise HTTPException(status_code=400, detail="Invalid variation index")
            
        rel_path = shot.draft_variations.pop(var_idx)
        # Resolve through the shared resolver so a doctored manifest path cannot
        # make this unlink an arbitrary file.
        abs_path = config.resolve_media(rel_path, scene_id)
        if abs_path is not None:
            abs_path.unlink()


        # Reset chosen variation
        if shot.chosen_variation == var_idx:
            shot.chosen_variation = 0 if shot.draft_variations else None
            shot.draft_image = shot.draft_variations[0] if shot.draft_variations else None
        elif shot.chosen_variation is not None and shot.chosen_variation > var_idx:
            shot.chosen_variation -= 1
            
        save_current_project(sb)
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/delete_video/{var_idx}")
def delete_video_variation(scene_id: str, var_idx: int):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
            
        video_vars = getattr(shot, "video_variations", [])
        if not video_vars or var_idx < 0 or var_idx >= len(video_vars):
            raise HTTPException(status_code=400, detail="Invalid video variation index")
            
        rel_path = video_vars.pop(var_idx)
        abs_path = config.resolve_media(rel_path, scene_id)
        if abs_path is not None:
            abs_path.unlink()


        if shot.video_clip == rel_path:
            shot.video_clip = video_vars[0] if video_vars else None
            
        save_current_project(sb)
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/script/generate")
async def generate_script_endpoint(request: Request):
    try:
        sb = get_current_project()
        data = await request.json()
        topic = data.get("topic")
        beats = data.get("beats")
        channel = data.get("channel") or sb.channel or "bestiary"
        
        if not topic:
            raise HTTPException(status_code=400, detail="Topic is required")

        def run_draft():
            log_job("script_draft", f"Generating AI script for topic: '{topic}'...")
            new_sb = script.generate_script(topic, num_beats=beats, channel=channel)
            new_sb.id = sb.id
            new_sb.title = sb.title
            save_current_project(new_sb)
            log_job("script_draft", f"Saved project '{new_sb.title}' with {len(new_sb.shots)} beats to workspace!")

        started = start_job("script_draft", run_draft)
        if not started:
            return JSONResponse(status_code=400, content={"ok": False, "error": "A script drafting job is already in progress."})

        return {"ok": True, "job_id": "script_draft", "status": "running"}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/script/from_chat")
async def script_from_chat_endpoint(request: Request):
    try:
        sb = get_current_project()
        data = await request.json()
        messages = data.get("messages", [])
        beats = data.get("beats")
        channel = data.get("channel") or sb.channel or "bestiary"

        def run_chat_draft():
            log_job("script_draft", "Generating AI script from chat conversation...")
            new_sb = script.generate_script_from_messages(messages, num_beats=beats, channel=channel)
            new_sb.id = sb.id
            new_sb.title = sb.title
            save_current_project(new_sb)
            log_job("script_draft", f"Saved project '{new_sb.title}' with {len(new_sb.shots)} beats to workspace!")

        started = start_job("script_draft", run_chat_draft)
        if not started:
            return JSONResponse(status_code=400, content={"ok": False, "error": "A script drafting job is already in progress."})

        return {"ok": True, "job_id": "script_draft", "status": "running"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/script/lock")
def lock_script_endpoint():
    try:
        sb = get_current_project()
        locked_sb = script.lock_script(sb)
        save_current_project(locked_sb)
        return {"ok": True}
    except ValueError as ve:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(ve)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/chat/develop")
async def chat_develop(request: Request):
    try:
        config.require_for("script")
        data = await request.json()
        messages = data.get("messages", [])
        channel = data.get("channel") or "bestiary"
        
        system_prompt = script.get_system_prompt(channel)
        reply = claude_chat(system_prompt, messages)
        return {"ok": True, "reply": reply}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/approve")
def approve_endpoint():
    try:
        sb = get_current_project()
        missing = [s.scene_id for s in sb.shots if not s.draft_image]
        if not sb.shots:
            return JSONResponse(status_code=400, content={"ok": False, "error": "No beats to approve."})
        if missing:
            return JSONResponse(status_code=400, content={"ok": False, "error": "These beats have no chosen image:", "scenes": missing})

        for s in sb.shots:
            s.approved = True
            if s.motion_type == MotionType.AI_VIDEO and not s.video_model:
                s.video_model = "seedance_2_0"
        sb.storyboard_approved = True
        save_current_project(sb)
        return {"ok": True, "gate_cleared": sb.gate_cleared(), "paid": [s.scene_id for s in sb.paid_shots()]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/assemble/{stage}")
def run_assemble_endpoint(stage: str, force_paid: bool = False):
    try:
        sb = get_current_project()
        
        if stage == "drafts":
            # Bulk draft-image generation. This is the step CLAUDE.md places
            # before Gate 1: the storyboard cannot be approved until every beat
            # has a chosen image, and until now the only way to get one from the
            # studio was to regenerate each beat by hand. The bulk generator
            # existed (assets.generate_drafts) but was reachable only from
            # pipeline.py, so the web flow deadlocked -- approval needed images,
            # and the other bulk path lives inside the render stage, which is
            # itself gated behind approval.
            if not sb.shots:
                return JSONResponse(status_code=400, content={"ok": False, "error": "No beats to illustrate. Draft a script first."})
            config.require_for("assets")

            def fn():
                assets.generate_drafts(
                    sb,
                    n=3,
                    backend=getattr(sb.render, "backend", assets.DEFAULT_BACKEND),
                    skip_existing=True,      # never re-pay for a beat that has drafts
                    save_after_each=True,    # a crash keeps the beats already bought
                    save_fn=save_current_project,
                    log=lambda m: log_job("drafts", m),
                )
                save_current_project(sb)
                missing = [s.scene_id for s in sb.shots if not s.draft_image]
                if missing:
                    log_job("drafts", f"Still missing images: {missing}")
                else:
                    log_job("drafts", "Every beat has a draft image — ready to approve.")

        elif stage == "narration":
            if not sb.script_locked:
                if sb.storyboard_approved:
                    sb.script_locked = True
                    save_current_project(sb)
                else:
                    return JSONResponse(status_code=400, content={"ok": False, "error": "Lock the script first."})
            
            def fn():
                voice = (getattr(sb, "voice_id", "") or "").strip() or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID
                log_job("narration", f"Synthesizing {len(sb.shots)} beat(s) with voice {voice} ...")
                clips = audio.synthesize_narration(sb)
                log_job("narration", f"{len(clips)} narration clip(s) written.")
                report: dict = {}
                changed = audio.sync_durations(sb, report=report)
                save_current_project(sb)
                total = sum(float(s.camera.duration) for s in sb.shots if s.camera)
                locked = report.get("locked") or []
                log_job(
                    "narration",
                    f"Synced {changed} shot duration(s) to audio"
                    + (f"; {len(locked)} locked beat(s) left as-is" if locked else "")
                    + f"; runtime now {total:.1f}s (~{total/60:.1f} min).",
                )
                # A locked beat shorter than its own VO lets narration bleed into
                # the next shot. Legal, but say it out loud -- this is the kind of
                # defect you otherwise find on the finished master.
                for o in report.get("overrun") or []:
                    log_job(
                        "narration",
                        f"  !! {o['scene_id']}: locked at {o['duration']:.1f}s but its "
                        f"narration runs {o['vo']:.1f}s — the voice will overrun into the "
                        f"next beat. Unlock it or raise it to {o['would_be']:.1f}s.",
                    )
                
        elif stage == "render":
            if not sb.storyboard_approved:
                return JSONResponse(status_code=400, content={"ok": False, "error": "Approve the storyboard first."})
            def fn():
                generate_fal_and_render(sb, force_paid=force_paid)
                # Auto-generate ambient SFX for all beats in the batch render pass
                # Must match where timeline.build and build_preview read SFX
                # from (episode_paths["sfx"]). These wrote to assets/sfx,
                # so every generated bed was invisible to the assembly.
                sfx_dir = config.episode_paths(sb.title)["sfx"]
                sfx_dir.mkdir(parents=True, exist_ok=True)
                for shot in sb.shots:
                    if shot.sfx:
                        dest = sfx_dir / f"{shot.scene_id}.mp3"
                        if not dest.exists():
                            try:
                                audio.generate_sfx_fal(shot.sfx, dest,
                                                       duration_seconds=shot.camera.duration)
                            except Exception as exc:  # noqa: BLE001
                                # One beat's ambience failing must not abandon
                                # the rest of the batch.
                                log_job("render", f"  !! {shot.scene_id} SFX failed: {exc}")
            
        elif stage == "preview":
            def fn():
                out, runtime = timeline.build_preview(sb)
                log_job("preview", f"Preview written: {_safe_rel_path(out)} ({runtime:.1f}s runtime)")

        elif stage == "timeline":
            def fn():
                # These stages used bare `print`, which goes to stdout and never
                # reaches the job log the UI polls — so a completed export said
                # nothing about what it produced or where.
                otio_path, fcpxml_path, runtime = timeline.build(sb)
                log_job("timeline", f"Runtime {runtime:.1f}s (~{runtime/60:.1f} min)")
                log_job("timeline", f"Wrote {otio_path.name}")
                if fcpxml_path:
                    log_job("timeline", f"Wrote {fcpxml_path.name} — download via /api/export/fcpxml")
                else:
                    log_job("timeline", "FCPXML export failed; the .otio is still valid.")


        else:
            raise HTTPException(status_code=404, detail="Assembly stage not found")
            
        if start_job(stage, fn):
            return {"ok": True, "stage": stage}
        return JSONResponse(status_code=409, content={"ok": False, "error": f"{stage} already running"})
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/voice/design")
async def voice_design_endpoint(request: Request):
    try:
        data = await request.json()
        gender = data.get("gender", "male")
        age = data.get("age", "middle_aged")
        accent = data.get("accent", "american")
        description = data.get("description", "A low-pitched raspy documentary narrator")
        sample_text = data.get("sample_text", "")
        
        res = audio.design_voice(description, sample_text, gender, age, accent)
        # Previews are auditions, not voices: each generated_voice_id must be
        # promoted via /api/voice/save before narration can use it.
        return {"ok": True, **res}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/voice/list")
def list_voices_endpoint():
    """Voices on the ElevenLabs account, plus which one this episode uses."""
    try:
        sb = get_current_project()
        return {
            "ok": True,
            "voices": audio.list_voices(),
            "selected": (getattr(sb, "voice_id", "") or "").strip(),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/voice/save")
async def save_voice_endpoint(request: Request):
    """Promote a design preview into a real voice and assign it to this episode.

    Without this step a designed voice is lost: previews are throwaway, and their
    generated_voice_id cannot be used for text-to-speech.
    """
    try:
        data = await request.json()
        gen_id = (data.get("generated_voice_id") or "").strip()
        name = (data.get("name") or "").strip()
        desc = (data.get("voice_description") or "").strip()
        if not gen_id or not name:
            raise HTTPException(status_code=400, detail="name and generated_voice_id are required")

        saved = audio.save_designed_voice(name, desc, gen_id)
        if not saved.get("voice_id"):
            raise HTTPException(status_code=502, detail="ElevenLabs returned no voice_id")

        sb = get_current_project()
        sb.voice_id = saved["voice_id"]
        save_current_project(sb)
        return {"ok": True, **saved, "assigned_to_episode": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/voice/sample")
async def voice_sample_endpoint(request: Request):
    """Audition a saved voice, optionally with unsaved stability/style values.

    /api/voice/design auditions brand-new voices, but there was no way to hear
    an existing voice or to hear what the sliders do — /api/voice/settings
    returns numbers and no audio. Sampling costs ElevenLabs characters, so this
    is explicit rather than firing on every slider move.
    """
    try:
        data = await request.json()
        sb = get_current_project()
        # Default to this episode's own first line: the most representative
        # thing to judge a narrator on is the script they will actually read.
        first = next((s.narration for s in sb.shots if (s.narration or "").strip()), "")
        text = (data.get("text") or first
                or "In the humid dark, something older than the village waits.").strip()

        mp3 = audio.sample_voice(
            text,
            voice_id=(data.get("voice_id") or "").strip() or None,
            stability=data.get("stability"),
            style=data.get("style_exaggeration"),
            storyboard=sb,
        )
        return {
            "ok": True,
            "audio_data_uri": "data:audio/mpeg;base64," + base64.b64encode(mp3).decode("ascii"),
            "chars": len(text),
            "text": text,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/voice/settings")
async def voice_settings_endpoint(request: Request):
    try:
        data = await request.json()
        # voice_id persists on the manifest; a designed voice used to be assigned
        # to a config module global and lost on the next container restart.
        if "voice_id" in data:
            sb = get_current_project()
            sb.voice_id = str(data["voice_id"]).strip()
            save_current_project(sb)
            voice_id = sb.voice_id
        else:
            voice_id = (getattr(get_current_project(), "voice_id", "") or "").strip()

        # Stability/style remain process-level tuning knobs, not episode state.
        if "stability" in data:
            config.ELEVENLABS_STABILITY = float(data["stability"])
        if "style_exaggeration" in data:
            config.ELEVENLABS_STYLE_EXAGGERATION = float(data["style_exaggeration"])
        return {
            "ok": True,
            "voice_id": voice_id or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID,
            "persisted": bool(voice_id),
            "stability": config.ELEVENLABS_STABILITY,
            "style_exaggeration": config.ELEVENLABS_STYLE_EXAGGERATION
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/audio/sfx/{scene_id}")
async def single_sfx_endpoint(scene_id: str):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        if not shot.sfx:
            raise HTTPException(status_code=400, detail="No SFX prompt set for this scene")
            
        sfx_dir = config.episode_paths(sb.title)["sfx"]
        sfx_dir.mkdir(parents=True, exist_ok=True)
        dest = sfx_dir / f"{scene_id}.mp3"
        
        out_path = audio.generate_sfx_fal(shot.sfx, dest, duration_seconds=shot.camera.duration)
        rel = _safe_rel_path(out_path)
        return {"ok": True, "path": rel}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/audio/narration/{scene_id}")
def single_narration_endpoint(scene_id: str):
    """Re-record one beat's narration.

    synthesize_narration skips anything already on disk -- correct for a batch,
    useless for "this line reads wrong". Delete first so the beat is genuinely
    re-recorded, and let it run as a job because TTS on a long beat is not
    instant.
    """
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        if not (shot.narration or "").strip():
            raise HTTPException(status_code=400, detail="This beat has no narration text.")
        if not sb.script_locked:
            raise HTTPException(status_code=400,
                                detail="Script gate: lock the script before recording narration.")

        dest = config.episode_paths(sb.title)["narration"] / f"{scene_id}.mp3"

        def fn():
            dest.unlink(missing_ok=True)
            out = audio.synthesize_narration(sb, only={scene_id})
            if not out:
                log_job("narration", f"{scene_id}: nothing written — check the narration text.")
                return
            # Durations are narration-led, so re-recording a beat changes its
            # length unless the user has pinned it.
            if not getattr(shot.camera, "duration_locked", False):
                report: dict = {}
                audio.sync_durations(sb, report=report)
                save_current_project(sb)
                log_job("narration", f"{scene_id}: re-recorded; duration now "
                                     f"{shot.camera.duration:.1f}s.")
            else:
                log_job("narration", f"{scene_id}: re-recorded; duration held at "
                                     f"{shot.camera.duration:.1f}s (locked).")

        start_job("narration", fn)
        return {"ok": True, "scene_id": scene_id}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/assemble/status")
def get_assemble_status():
    return {"ok": True, "jobs": get_jobs_status()}


# --- MEDIA SERVING ENDPOINTS ---

@app.get("/api/audio/peaks")
def audio_peaks():
    """Waveform envelopes for every beat's narration and SFX.

    Cached to a sidecar beside the manifest and keyed by (size, mtime) per file,
    so the expensive decode only happens when a clip actually changes. Without
    that this is 30 ffmpeg decodes on every visit to the Editing step.
    """
    try:
        sb = get_current_project()
        ep = config.episode_paths(sb.title)
        cache_path = config.MANIFEST_PATH.parent / "_peaks_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a bad cache must not break the view
            cache = {}

        out, dirty = {}, False
        for s in sb.shots:
            entry = {}
            for track, d in (("narration", ep["narration"]), ("sfx", ep["sfx"])):
                f = d / f"{s.scene_id}.mp3"
                if not f.is_file():
                    continue
                st = f.stat()
                key = f"{s.scene_id}:{track}:{st.st_size}:{int(st.st_mtime)}"
                if key in cache:
                    entry[track] = cache[key]
                else:
                    env = audio.peaks(f)
                    if env:
                        cache[key] = env
                        entry[track] = env
                        dirty = True
            if entry:
                out[s.scene_id] = entry

        if dirty:
            # Keep only what this episode currently references, so the sidecar
            # does not grow without bound as clips are regenerated.
            live = {f"{sid}:{tr}" for sid, e in out.items() for tr in e}
            cache = {k: v for k, v in cache.items()
                     if ":".join(k.split(":")[:2]) in live}
            try:
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                print(f"peaks: could not write cache ({exc})")

        return {"ok": True, "peaks": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/audio/transcode")
def transcode_audio_endpoint(normalize_sfx: bool = False):
    """Re-encode this episode's uncompressed audio to MP3, in place.

    Generators hand back WAV (ACE-Step for music, stable-audio for SFX), which is
    ~10x the bytes and is paid for on every export bundle, GCS read and mix.
    New generations are MP3 already; this migrates what already exists.

    ``normalize_sfx`` additionally seats SFX at a fixed loudness. Off by default
    because it changes how the audio sounds — the raw stems here vary by 13 dB
    between beats, which one master fader cannot fix, but that is a creative
    call rather than a storage one.
    """
    try:
        sb = get_current_project()
        ep = config.episode_paths(sb.title)

        def fn():
            saved = 0
            targets: list[tuple[Path, str, float | None]] = []
            for d, br in ((ep["sfx"], "128k"), (ep["narration"], "128k")):
                if d.is_dir():
                    lufs = -23.0 if (normalize_sfx and d == ep["sfx"]) else None
                    targets += [(p, br, lufs) for p in sorted(d.iterdir()) if p.is_file()]
            if sb.music_track:
                mp = config.AUDIO_POOL / sb.music_track
                if mp.is_file():
                    targets.append((mp, "192k", None))

            for p, br, lufs in targets:
                if p.suffix.lower() == ".mp3" and audio.is_mp3(p) and lufs is None:
                    continue
                before = p.stat().st_size
                try:
                    out = audio.transcode_to_mp3(p, bitrate=br, normalize_lufs=lufs,
                                                 log=lambda m: log_job("transcode", m))
                except Exception as exc:  # noqa: BLE001
                    log_job("transcode", f"  !! {p.name}: {exc}")
                    continue
                saved += before - out.stat().st_size
                # The manifest names the bed by filename; a rename must follow it
                # or the mix silently loses the music.
                if sb.music_track and p.name == sb.music_track and out.name != p.name:
                    sb.music_track = out.name
                    save_current_project(sb)
                    log_job("transcode", f"music_track -> {out.name}")
            log_job("transcode", f"Done. Reclaimed {saved/1e6:.1f} MB.")

        start_job("transcode", fn)
        return {"ok": True, "stage": "transcode"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/metadata")
def get_metadata():
    """Saved publishing metadata for this episode, if it has been drafted."""
    try:
        sb = get_current_project()
        md = metadata.load_saved(sb)
        return {"ok": True, "metadata": md.to_dict() if md else None}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/metadata/generate")
def generate_metadata_endpoint():
    """Draft title / description / chapters / tags from the locked script."""
    try:
        sb = get_current_project()
        if not sb.script_locked:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "Metadata is drafted from the locked script. Lock the script first.",
            })

        def fn():
            md = metadata.generate(sb, log=lambda m: log_job("metadata", m))
            p = metadata.save(md, sb)
            log_job("metadata", f"Wrote {_safe_rel_path(p)} — "
                                f"{len(md.chapters)} chapter(s), {len(md.tags)} tag(s).")

        start_job("metadata", fn)
        return {"ok": True, "stage": "metadata"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/metadata")
async def update_metadata(request: Request):
    """Persist hand-edited metadata. The draft is a starting point, not the copy."""
    try:
        sb = get_current_project()
        data = await request.json()
        md = metadata.Metadata.from_dict(data)
        p = metadata.save(md, sb)
        return {"ok": True, "saved": _safe_rel_path(p), "metadata": md.to_dict()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/export/bundle")
def build_export_bundle():
    """Pack the FCPXML plus every asset it references into one ZIP.

    The plain .fcpxml points at absolute container paths, so on any other machine
    every clip is offline. This rewrites them relative and ships the media.
    """
    try:
        sb = get_current_project()

        def fn():
            p = bundle.build(sb, log=lambda m: log_job("bundle", m))
            log_job("bundle", f"Ready: {_safe_rel_path(p)}")

        start_job("bundle", fn)
        return {"ok": True, "stage": "bundle"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/export/bundle")
def download_export_bundle():
    """Stream the bundle.

    Deliberately streamed rather than served with FileResponse: Cloud Run rejects
    a response that declares a large Content-Length ("Response size was too
    large"), which is exactly what FileResponse does. Yielding chunks with no
    Content-Length sends it chunked instead, which is not subject to that cap —
    a full episode bundle is a few hundred MB and will never fit under it.

    The cost is that the browser cannot show a progress percentage. That is
    worth it for a download that otherwise 500s.
    """
    sb = get_current_project()
    slug = config.episode_paths(sb.title)["slug"]
    path = (config.MANIFEST_PATH.parent / f"{slug}_bundle.zip").resolve()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No bundle yet — build it first (POST /api/export/bundle).",
        )

    def _chunks(p: Path, size: int = 1024 * 1024):
        # 1 MiB reads: the file lives on the GCS FUSE mount, where many small
        # reads are many network round trips.
        with open(p, "rb") as fh:
            while True:
                blk = fh.read(size)
                if not blk:
                    break
                yield blk

    return StreamingResponse(
        _chunks(path),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "X-Bundle-Bytes": str(path.stat().st_size),
        },
    )


@app.get("/api/export/{kind}")
def download_timeline_export(kind: str):
    """Download this episode's Resolve timeline (Gate 2's actual deliverable).

    Deliberately not routed through /media/: those paths are restricted to
    generated media (image/video/audio suffixes) inside the asset subtrees, and
    widening that allowlist to serve XML from the project directory would undo
    the containment that keeps .env and credentials out of reach. A dedicated
    route with a fixed filename is both safer and gives the browser a real
    download rather than an inline render.
    """
    kind = kind.lower().lstrip(".")
    if kind not in ("fcpxml", "otio"):
        raise HTTPException(status_code=404, detail="Unknown export type")

    sb = get_current_project()
    slug = config.episode_paths(sb.title)["slug"]
    path = (config.MANIFEST_PATH.parent / f"{slug}.{kind}").resolve()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"No {kind} yet — run the timeline stage first.",
        )
    return FileResponse(
        path,
        media_type="application/xml" if kind == "fcpxml" else "application/octet-stream",
        filename=f"{slug}.{kind}",
    )


@app.get("/media/{filepath:path}")
def serve_media_files(filepath: str):
    """Serve a generated asset.

    Resolution and containment both live in ``config.resolve_media``, which
    refuses traversal and confines the result to the project's media roots — so
    this endpoint cannot be walked back up to .env or the service-account key.
    """
    resolved = config.resolve_media(filepath)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Media not found: {filepath}")
    return FileResponse(
        resolved, headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"}
    )


@app.get("/assets/{filepath:path}")
def serve_assets_files(filepath: str):
    return serve_media_files(f"assets/{filepath}")


@app.get("/references/{filepath:path}")
def serve_references_files(filepath: str):
    return serve_media_files(f"references/{filepath}")


@app.get("/render/{filepath:path}")
def serve_render_files(filepath: str):
    return serve_media_files(f"render/{filepath}")


# Serving the static UI (Next.js static export fallback handler)
from fastapi.staticfiles import StaticFiles

static_dir = WORKSPACE_ROOT / "frontend" / "out"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
else:
    @app.get("/")
    def serve_spa():
        return {"message": "Automation API running. Serve frontend static build out of frontend/out folder."}
