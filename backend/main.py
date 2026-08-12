"""FastAPI Application entry point for the YouTube Video Automation Studio.

Routes requests to the Firestore database models, ElevenLabs audio generator,
fal.ai media API, and DaVinci Resolve OTIO exporter.
"""

from __future__ import annotations

import base64
import os
import re
import json
import datetime as _dt
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
from . import config, manifest, script, assets, audio, motion, timeline, sizzle, metadata, bundle, ledger
from . import director, spike_identity, planner, capabilities, casting, characters
from . import stages as stagemod
from . import projects
from .manifest import Storyboard, Shot, MotionType, Camera, RenderConfig, db
from .pipeline_worker import start_job, get_jobs_status, log_job
from .ffmpeg_bin import ffmpeg_bin, ffprobe_bin

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

    # Point the process config at the saved active project before serving anything.
    # Nothing did this, and cloudbuild.yaml sets no MANIFEST_PATH, so a cold
    # container started on /app/... -- which EXISTS, because the Dockerfile COPYs
    # the repo in, so reads succeeded and there was no error to notice. The first
    # request to a fresh container wrote its character anchor to /app, reported
    # "/app/characters.json" back as the path it had written, read it back
    # successfully, and lost it at scale-to-zero. Nothing under /gcs ever saw it.
    try:
        active = get_active_manifest_path()
        if active:
            config.set_active_manifest(active)
            print(f"Startup: active project -> {config.MANIFEST_PATH}")
    except Exception:
        print("Startup warning: could not sync active project:")
        print(traceback.format_exc())

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


@app.middleware("http")
async def bind_project_context(request: Request, call_next):
    """Bind this request's project identity for its whole lifetime.

    The target comes from the client when it says which project it means
    (``X-Project-Id`` header or a ``project_id`` query parameter) and otherwise
    falls back to the active-project pointer. Either way it is resolved **once,
    here**, and every path derived downstream comes from that one context.

    Two things this buys, both contract §11.3:

    * a concurrent ``/api/project/select`` can no longer retarget a request that
      is already in flight -- the switch rebinds the pointer file, not this
      request's context;
    * a client that names a project which is not the active one is answered
      about the project it named, or refused, rather than being quietly served
      another project's data under the id it asked for.
    """
    requested = (request.headers.get("X-Project-Id")
                 or request.query_params.get("project_id") or "").strip()
    try:
        ctx = _context_for(requested)
    except projects.UnknownProject as exc:
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": str(exc),
                                     "project_id": requested})
    token = projects.bind(ctx)
    try:
        response = await call_next(request)
    finally:
        projects.reset(token)
    # Lets the client discard a reply that arrived after it switched projects.
    response.headers["X-Project-Id"] = ctx.project_id
    return response


def _context_for(project_id: str = "") -> projects.ProjectContext:
    """Resolve a client-supplied project id, or the active project when blank.

    "Active" means ``config.manifest_path()``, not the pointer file directly.
    Startup seeds it from the pointer and ``/api/project/select`` writes both, so
    the two agree in production -- but reading the process value keeps a single
    notion of "current project" for unbound callers instead of adding a second
    one that can disagree with it.
    """
    active = projects.ProjectContext.from_manifest(config.manifest_path())
    if not project_id:
        return active
    if active.project_id == project_id:
        return active
    for entry in _scan_projects():
        cand = projects.ProjectContext.from_manifest(entry["rel"])
        if cand.project_id == project_id:
            return cand
    # Never silently serve a different project under the id the client asked
    # for. Refusing is the only answer that cannot corrupt the wrong project.
    raise projects.UnknownProject(f"Unknown project id: {project_id}")


WORKSPACE_ROOT = Path(config.ROOT).resolve()
ACTIVE_PROJECT_FILE = Path("/gcs/.active_project") if Path("/gcs").exists() else Path(".active_project")
IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "frontend", "backend"}


def get_project_id_from_path(path: str | Path) -> str:
    """Derive a clean, unique Firestore document ID from a manifest path.

    Delegates so the id a request is bound to and the id a document is stored
    under can never drift apart.
    """
    return projects.project_id_for(path)


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
    """Save the active manifest path AND repoint the process config to match.

    These were two separate steps and only one of them happened here, so after
    /api/project/select the pointer file said one project while
    config.MANIFEST_PATH / ASSETS / REFERENCES_DIR / CHARACTERS_CONFIG still named
    the previous one -- until some later request happened to call
    get_current_project(). Anything that wrote before that (the /api/characters
    handlers, put_director_plan) wrote to the wrong project and reported the wrong
    path back as if it had succeeded.
    """
    resolved = str(Path(path).resolve())
    ACTIVE_PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_PROJECT_FILE.write_text(resolved, encoding="utf-8")
    config.set_active_manifest(resolved)


def get_current_project() -> Storyboard:
    """The storyboard for THIS request's project, with robust fallback creation.

    Reads the bound context first. Under HTTP a context is always bound by
    ``bind_project_context``, so this no longer consults the active-project
    pointer at all -- which is what stops a mid-request project switch from
    changing the answer. Only unbound callers (the CLI, tests) fall through to
    the pointer.
    """
    ctx = projects.bound()
    active_path = str(ctx.manifest_path) if ctx else get_active_manifest_path()
    f_id = ctx.project_id if ctx else get_project_id_from_path(active_path)
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


    # Only unbound callers repoint the process globals. Doing this while a
    # context is bound would let one request's read mutate what a *concurrent*
    # request resolves -- reintroducing the shared-global race from the other
    # direction.
    if ctx is None:
        config.set_active_manifest(active_path)
    return sb


def save_current_project(sb: Storyboard):
    """Persist a storyboard to the project THIS request or job belongs to.

    The destination is the bound context, never the active-project pointer.

    This wrapper used to pass ``get_active_manifest_path()`` explicitly, which
    stepped straight over ``manifest.save``'s bound-aware default -- so a request
    or background job working on project B wrote B's storyboard over project A's
    manifest whenever A happened to be the globally active one. A whole film's
    approvals, selections and timing replaced by another's, reported as success.
    Hardening the primitive was not enough while its main caller overrode it.

    The Firestore id is taken from the same context for the same reason: the
    document and the file must not be able to describe different projects.
    """
    ctx = projects.bound()
    if ctx is not None:
        # Keep the document id and the file in agreement with the bound project.
        sb.id = ctx.project_id
    try:
        manifest.save_project(sb)
    except Exception as fe:
        print(f"Warning: Firestore save_project failed: {fe}")
    # Save back to local/GCS JSON for CLI & local sync. No explicit path: the
    # default already resolves the bound project, and naming one here is exactly
    # how this went wrong before.
    manifest.save(sb)


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
            # _trash holds retired projects. They keep their manifests, so
            # without this they reappear in the sidebar the moment they are
            # deleted -- and the studio could be pointed back at one.
            skip = set(IGNORE_DIRS) | {"references", "source", "_trash"}
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
                    # The id the client sends as X-Project-Id to target this
                    # project explicitly instead of relying on the pointer.
                    "project_id": get_project_id_from_path(str(resolved)),
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
    if config.references_config().exists():
        return json.loads(config.references_config().read_text(encoding="utf-8"))
    return {}


def _save_ref_registry(reg: dict) -> None:
    config.references_config().write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")


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
    if current <= 0 or abs(shortfall) < 0.1:
        return

    if shortfall < 0:
        # The clip is LONGER than its beat, which happens whenever narration is
        # re-recorded shorter and sync_durations shrinks the slot. This function
        # only ever padded, so the over-long clip survived forever: the rough
        # cut's stale-clip cleanup deliberately excludes AI_VIDEO, and the
        # re-bill keep-branch calls straight back into here. build_preview then
        # concatenates with -c copy while laying audio against manifest offsets,
        # so every beat AFTER it drifts out of sync and -shortest truncates the
        # tail. timeline.build trims correctly via source_range, so the FCPXML
        # and the preview disagreed about the same cut.
        trimmed = path.with_name(f"{path.stem}__trim.mp4")
        try:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-v", "error", "-i", str(path),
                 "-t", f"{float(target_seconds):.3f}",
                 "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
                 str(trimmed)],
                check=True,
            )
            shutil.copyfile(trimmed, path)
            trimmed.unlink(missing_ok=True)
            log_job("render", f"  trimmed to beat length: {current:.1f}s -> "
                              f"{float(target_seconds):.1f}s")
        except Exception as exc:  # noqa: BLE001
            trimmed.unlink(missing_ok=True)
            log_job("render", f"  !! could not trim {path.name} to its beat: {exc}")
        return

    padded = path.with_name(f"{path.stem}__padded.mp4")
    try:
        subprocess.run(
            [ffmpeg_bin(), "-y", "-v", "error", "-i", str(path),
             "-vf", f"tpad=stop_mode=clone:stop_duration={shortfall:.3f}",
             "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
             str(padded)],
            check=True,
        )
        # Copy the bytes over rather than renaming: a rename on the GCS FUSE
        # mount is a server-side copy plus delete and is not always permitted,
        # and the temporary file is disposable either way.
        shutil.copyfile(padded, path)
        padded.unlink(missing_ok=True)
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
    # copyfile, never copy2: copy2 also copies metadata, which means chmod and
    # utime on the destination, and the GCS FUSE mount rejects both with
    # "[Errno 1] Operation not permitted". Only the bytes matter here.
    shutil.copyfile(src_path, dest_path)
    log_job("render", f"  {shot.scene_id}: clip placed ({dest_path.stat().st_size:,} bytes)")
    if shot.camera:
        _pad_clip_to_beat(dest_path, float(shot.camera.duration))
    try:
        frame_out_path = config.assets_dir() / shot.scene_id / f"final_frame_{shot.scene_id}.png"
        assets.extract_final_frame(dest_path, frame_out_path)
    except Exception as e:
        print(f"Error extracting final frame for {shot.scene_id}: {e}")


_SHOT_ASSET_FIELDS = (
    "draft_variations", "draft_image", "chosen_variation",
    "video_variations", "video_clip", "hero_clip",
)


def save_shot_assets(shot) -> None:
    """Persist only the asset fields this job owns, onto the CURRENT manifest.

    A render holds one Storyboard object for twenty-odd minutes. Writing that
    whole object back clobbers everything another stage changed in the meantime:
    narration syncing beat durations and locking the script were both silently
    reverted this way, leaving every beat at the 6.0s default and the script
    unlocked -- an episode that looked complete and was 150s of wrong timing.

    Re-reading and copying across only the generated-asset fields keeps the two
    jobs from fighting over the same file.
    """
    current = get_current_project()
    target = next((s for s in current.shots if s.scene_id == shot.scene_id), None)
    if target is None:
        return
    for f in _SHOT_ASSET_FIELDS:
        if hasattr(shot, f):
            setattr(target, f, getattr(shot, f))
    save_current_project(current)


def generate_fal_and_render(sb: Storyboard, force_paid: bool = False, log=None) -> None:
    """Render every beat. Local tiers are free and always redone; paid ai_video
    beats that already have a placed clip are kept unless ``force_paid``.

    Without that guard, any re-run — a camera tweak, a fixed FX, a retry after
    one beat failed — silently re-billed every ai_video beat through fal.
    """
    # log_job() drops lines for a job that was never started, so when the rough
    # cut called this every per-beat line vanished and a 40-minute render looked
    # frozen on one status line. Callers pass their own logger; the fallback
    # writes to the "render" job.
    #
    # This fallback used to read `lambda m: log(m)`, which calls itself — the name
    # it closes over IS the lambda by the time it runs. It stayed dormant because
    # every caller added since passes a logger explicitly; only the standalone
    # /api/assemble/render endpoint calls this bare, and the first time it ran it
    # died on RecursionError one beat in.
    log = log or (lambda m: log_job("render", m))

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
            # A beat assembled from director coverage owns its clip. Rendering
            # over it would silently discard the coverage and leave the plan
            # describing a file that no longer matches it — the exact shape of
            # failure this pipeline keeps producing. Skip loudly instead.
            if director.has_locked_coverage(shot.scene_id):
                log(f"{shot.scene_id}: covered by a locked director plan — skipped "
                    f"(recompile coverage to rebuild this beat).")
                prev_extracted_frame = None
                prev_video_dest_path = None
                continue

            if getattr(shot, "hero_clip", False):
                log(f"{shot.scene_id}: Already has imported hero clip - keeping it, not re-rendering.")
                prev_extracted_frame = None
                prev_video_dest_path = None
                continue
            
            # Ensure still image draft is generated
            if not shot.draft_image:
                backend = getattr(shot, "image_model", None) or getattr(sb.render, "backend", None) or "nano2"
                log(f"Generating drafts for {shot.scene_id} using {backend}...")
                assets.generate_for_shot(shot, n=_takes(sb), backend=backend, render=sb.render)
                shot.chosen_variation = 0
                shot.draft_image = shot.draft_variations[0]
                save_shot_assets(shot)
            
            is_ai = (shot.motion_type == MotionType.AI_VIDEO)

            if is_ai and not force_paid and (shot.video_clip or shot.video_variations):
                placed = out_dir / f"{shot.scene_id}.mp4"
                # The clip may be paid for and on disk without being PLACED:
                # generate_shot_video explicitly tolerates placement failing --
                # it persists the variation, catches, and tells the user "the clip
                # was generated and kept". That state passed the manifest test and
                # failed the file test, so the next batch called fal again for a
                # clip already sitting under assets/. Re-place before re-billing.
                if not placed.exists():
                    for cand in ([shot.video_clip] if shot.video_clip else []) + list(shot.video_variations or []):
                        if cand and config.resolve_media(cand, shot.scene_id):
                            log(f"[{idx}/{total}] {shot.scene_id}: paid clip already "
                                f"generated but never placed — placing it instead of "
                                f"re-billing.")
                            set_active_video_clip(sb, shot, cand, out_dir)
                            break
                if placed.exists():
                    log_job(
                        "render",
                        f"[{idx}/{total}] {shot.scene_id}: paid clip already rendered — "
                        f"keeping it (re-run with force_paid=true to re-bill).",
                    )
                    # Keeping the paid pixels does not mean keeping a stale length.
                    # This branch used to skip the beat outright, so a Tier-C beat
                    # whose duration changed after it was rendered kept a clip fitted
                    # to the old timing — and since every other beat re-renders, it
                    # was the one beat that stayed wrong. Re-fitting is a local
                    # freeze-frame pad: free, and it re-bills nothing.
                    try:
                        _pad_clip_to_beat(placed, float(shot.camera.duration))
                    except Exception as exc:  # noqa: BLE001
                        log_job("render", f"  (could not refit {shot.scene_id}: {exc})")
                    prev_extracted_frame = None
                    prev_video_dest_path = placed
                    continue

            if is_ai:
                video_key = getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
                model_endpoint = resolve_video_model_endpoint(video_key)
                log(f"[{idx}/{total}] PAID video for {shot.scene_id} via {model_endpoint} (chaining: {chaining_mode}) ...")
                target_dur = float(getattr(shot.camera, "duration", 6.0))

                gen_audio = shot.video_audio
                if gen_audio is None:
                    gen_audio = getattr(sb.render, "video_audio", True)

                # Every model's own spelling and its own limits, from the schemas.
                # The hand-written clamp this replaces sent kling/wan/luma no
                # duration at all, sent seedance an illegal "3", and capped
                # everything at 10s though seedance and wan reach 15.
                dur_args, dur_note = capabilities.video_arguments(
                    video_key, target_dur, generate_audio=bool(gen_audio),
                    cap_to_ceiling=True)
                if dur_note:
                    log(f"  {shot.scene_id}: {dur_note}")
                dur_int = capabilities.clamp_duration(video_key, target_dur)

                motion_prompt = shot.motion_prompt or f"Cinematic motion, high-quality, authentic detail, {shot.prompt}"
                if f"{dur_int}s" not in motion_prompt and "second" not in motion_prompt:
                    motion_prompt = f"{motion_prompt} (duration: ~{dur_int} seconds)"

                arguments = {"prompt": motion_prompt, **dur_args}

                # Native Video Extend
                # These are image-to-video endpoints: a start image is required on
                # EVERY path, including the extend path. Sending only `video_url`
                # is rejected outright —
                #   {'loc': ['body','image_url'], 'msg': 'Field required'}
                # — which is why native_extend failed on precisely the condition
                # that makes it fire: the previous beat having a clip to chain from.
                # It sat behind two defaults (native_extend + seedance_2_0) and only
                # surfaced when a whole-episode re-render hit a Tier-C beat whose
                # predecessor was already rendered.
                extending = (
                    chaining_mode == "native_extend"
                    and prev_video_dest_path and prev_video_dest_path.exists()
                    and video_key in ("seedance_2_0", "luma_dream_machine", "hunyuan_video")
                )

                # OpenCV final frame or initial still
                if chaining_mode != "independent" and prev_extracted_frame and prev_extracted_frame.exists():
                    local_image_path = prev_extracted_frame
                    log(f"Continuous flow: chaining from final frame -> {local_image_path.name}")
                else:
                    local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                    if not local_image_path or not local_image_path.exists():
                        log(f"Still image draft not found for {shot.scene_id}, generating still drafts...")
                        try:
                            assets.generate_for_shot(shot, n=_takes(sb), backend=sb.render.backend, render=sb.render)
                            shot.chosen_variation = 0
                            shot.draft_image = shot.draft_variations[0]
                            save_shot_assets(shot)
                            local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                        except Exception as exc:
                            log(f"  !! Failed to generate still draft for {shot.scene_id}: {exc}")
                            continue

                if not local_image_path or not local_image_path.exists():
                    log(f"  !! Still image draft file missing on disk for {shot.scene_id}: {shot.draft_image}")
                    continue

                log(f"Uploading starting image {local_image_path.name}...")
                arguments["image_url"] = fal_client.upload_file(str(local_image_path))

                if extending:
                    log(f"Native Video Extend: also extending from {prev_video_dest_path.name}...")
                    arguments["video_url"] = fal_client.upload_file(str(prev_video_dest_path))
            
                log(f"Triggering fal.ai API with prompt: {motion_prompt[:80]}...")
                result = fal_client.subscribe(model_endpoint, arguments=arguments, with_logs=True)
                video_url = result.get("video", {}).get("url") or result.get("file", {}).get("url")
                if not video_url:
                    raise RuntimeError(f"No video URL returned from fal.ai for {shot.scene_id}")

                import time
                shot_assets_dir = config.assets_dir() / shot.scene_id
                shot_assets_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                var_count = len(getattr(shot, "video_variations", []))
                local_video_name = f"video_{timestamp}_{var_count}.mp4"
                local_video_path = shot_assets_dir / local_video_name

                log(f"Downloading generated video from {video_url} to {local_video_path}...")
                assets._download(video_url, local_video_path)

                video_rel_path = f"assets/{shot.scene_id}/{local_video_name}"
                if not hasattr(shot, "video_variations") or shot.video_variations is None:
                    shot.video_variations = []
                shot.video_variations.append(video_rel_path)

                set_active_video_clip(sb, shot, video_rel_path, out_dir)
                save_shot_assets(shot)

                dest_video_path = out_dir / f"{shot.scene_id}.mp4"
                prev_video_dest_path = dest_video_path
                log(f"Successfully generated video for {shot.scene_id}")

                try:
                    frame_out_path = config.assets_dir() / shot.scene_id / f"final_frame_{shot.scene_id}.png"
                    prev_extracted_frame = assets.extract_final_frame(dest_video_path, frame_out_path)
                except Exception as exc:
                    log(f"Warning: Failed to extract final frame for continuous chaining on {shot.scene_id}: {exc}")
                    prev_extracted_frame = None
            else:
                log(f"[{idx}/{total}] Rendering {shot.scene_id} locally ({shot.motion_type.value}) ...")
                motion.render_shot(shot, fps=motion.DEFAULT_FPS, height=motion.DEFAULT_HEIGHT,
                                   out_dir=out_dir, placeholder=False,
                                   motion_cfg=getattr(sb, "motion", None), storyboard=sb)
                prev_extracted_frame = None
                prev_video_dest_path = None
            
        except Exception as exc:  # noqa: BLE001 -- resilient batch
            failures.append(shot.scene_id)
            log(f"  !! {shot.scene_id} FAILED: {exc.__class__.__name__}: {exc}")
            prev_extracted_frame = None
            prev_video_dest_path = None

    if failures:
        log(f"Finished with {len(failures)} failed beat(s): {failures} -- re-run to retry just these.")
    else:
        log(f"All {total} beat(s) rendered.")


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
            # Resolved layers with playable urls, so the node graph does not need
            # one request per beat to draw them.
            resolved = []
            for lay in audio.resolve_sfx_layers(s, ep["sfx"]):
                d = asdict(lay)
                f = Path(lay.file) if lay.file else (ep["sfx"] / f"{s.scene_id}.mp3")
                if not f.is_absolute():
                    f = config.resolve_media(str(f), s.scene_id) or f
                d["url"] = config.rel_media_path(f) if f and Path(f).is_file() else None
                resolved.append(d)
            s_dict["sfx_layers_resolved"] = resolved
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

        # The preview's own timing, plus whether it still describes this cut.
        # Comparing the sidecar's per-beat durations to the live manifest is the
        # only honest staleness check: mtime alone would call a preview stale
        # after any unrelated manifest write (a gain, a grade, a note).
        preview_meta = None
        if preview_url:
            try:
                preview_meta = json.loads((ep["render"] / "_preview.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — a preview built before this existed
                preview_meta = None
        if preview_meta:
            live = [(s.scene_id, round(float(s.camera.duration), 2)) for s in sb.shots]
            baked = [(b["scene_id"], round(float(b["duration"]), 2))
                     for b in preview_meta.get("beats", [])]
            preview_meta["stale"] = live != baked
            preview_meta["live_runtime"] = round(sum(d for _, d in live), 3)
        
        # Same location timeline.build writes to: the project directory.
        fcpxml_file = config.project_dir() / f"{ep['slug']}.fcpxml"
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
            # Stale-response rejection: the client compares this to the project
            # it currently has selected and discards replies that lost the race.
            "project_id": projects.require().project_id,
            "project": {
                "id": sb.id,
                "title": sb.title,
                "channel": sb.channel,
                "cultural_origin": sb.cultural_origin,
                "script_locked": sb.script_locked,
                "storyboard_approved": sb.storyboard_approved,
                "voice_id": getattr(sb, "voice_id", "") or "",
                # Which narrator this episode uses. Persisted correctly but absent
                # from this payload, so the studio had no way to show it — the
                # third hand-maintained field list this one value had to be added
                # to (dataclass, from_dict, here).
                "vo_profile": getattr(sb, "vo_profile", "") or "",
                # The narrator's display NAME, already resolved. The UI must
                # never hard-code one (contract §4); it renders this.
                "narrator_name": manifest.narrator_name(sb),
                "narrator_name_is_default": not (getattr(sb, "narrator_name", "") or "").strip(),
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
            "preview_meta": preview_meta,
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


@app.get("/api/stages")
def get_stages():
    """The six-stage spine: status, blocking reasons, hints and the next action.

    The single source for stage gating. The frontend used to assemble this from
    counts it held locally, which is exactly the "no false success" failure the
    contract forbids (§11.4) -- a client that computes its own idea of "approved"
    can disagree with the server about whether money is allowed to be spent.
    """
    try:
        sb = get_current_project()
        ep = config.episode_paths(sb.title)

        def _plan_status(beat_id: str):
            p = director.load_plan(beat_id)
            return p.status if p else None

        data = stagemod.payload(sb, ep, _plan_status)
        data["ok"] = True
        data["narrator_name"] = manifest.narrator_name(sb)
        data["project_id"] = sb.id
        data["project_title"] = sb.title
        return data
    except Exception as e:  # noqa: BLE001
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

        # The job-scoped project context this comment used to be waiting for has
        # landed: jobs capture a ProjectContext at enqueue and rebind it in the
        # worker thread, so a switch mid-render no longer sends paid clips into
        # the other project's assets/. The guard stays as belt-and-braces while
        # that isolation is still being reviewed -- see refuse_if_jobs_running.
        refuse_if_jobs_running("switching projects")
        set_active_manifest_path(str(p))
        return {"ok": True}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# Directories that sit beside projects and are shared by all of them. A delete
# must never be able to name one of these as its target.
_PROTECTED_DIRS = {"audio_pool", "models", "_trash", "_inspect", "references"}


@app.post("/api/project/delete")
async def delete_project(request: Request):
    """Retire a project by moving it to _trash/. Not an unlink.

    A finished episode is 15-25 dollars of paid generation and hours of review.
    Deleting it outright means one mis-click destroys work that cannot be
    regenerated identically, so this moves the directory aside and reports where
    it went. ``purge: true`` removes it permanently, and is deliberately a second
    explicit decision rather than a flag on the first.
    """
    try:
        data = await request.json()
        rel_path = (data.get("rel") or "").strip()
        confirm = (data.get("confirm") or "").strip()
        purge = bool(data.get("purge"))
        if not rel_path:
            raise HTTPException(status_code=400, detail="Missing project path")

        p = Path(rel_path).resolve()
        roots = [WORKSPACE_ROOT.resolve()]
        gcs_root = Path("/gcs")
        if gcs_root.exists():
            roots.append(gcs_root.resolve())
        root = next((r for r in roots if r in p.parents), None)
        if root is None:
            raise HTTPException(status_code=400, detail="Project path is outside the workspace")
        if p.name != "storyboard_manifest.json" or not p.is_file():
            raise HTTPException(status_code=404, detail="No project manifest at that path")

        target = p.parent
        # The manifest must live in a project directory, never a channel folder,
        # a shared directory, or a workspace root itself.
        if target in roots or target.name in _PROTECTED_DIRS:
            raise HTTPException(status_code=400,
                                detail=f"Refusing to delete {target.name}: not a project directory")
        if target.parent in roots and target.name in _PROTECTED_DIRS:
            raise HTTPException(status_code=400, detail="Refusing to delete a shared directory")
        # Never delete a directory that contains OTHER projects. A stray manifest
        # dropped in a channel folder would otherwise make that folder the
        # target, taking every episode inside it along.
        nested = [m for m in target.rglob("storyboard_manifest.json") if m.resolve() != p]
        if nested:
            raise HTTPException(
                status_code=400,
                detail=f"Refusing: {target.name} contains {len(nested)} other project(s). "
                       f"Delete them individually.",
            )

        # Typed confirmation, matched against the project's own title or folder
        # name. A destructive action should require naming the thing.
        try:
            title = (manifest.load(p).title or "").strip()
        except Exception:  # noqa: BLE001 — an unreadable manifest is still deletable
            title = ""
        expected = {title.lower(), target.name.lower()} - {""}
        if confirm.lower() not in expected:
            raise HTTPException(
                status_code=400,
                detail=f"Type the project name to confirm. Expected one of: "
                       f"{', '.join(sorted(expected)) or target.name}",
            )

        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        was_active = config.MANIFEST_PATH.resolve() == p
        if was_active:
            # Deleting the active project repoints the globals to whatever remains.
            refuse_if_jobs_running("deleting the active project")

        if purge:
            shutil.rmtree(target)
            moved_to = None
        else:
            trash = root / "_trash"
            trash.mkdir(parents=True, exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = trash / f"{target.parent.name}__{target.name}__{stamp}"
            _move_tree(target, dest)
            moved_to = _safe_rel_path(dest)

        # Never leave the studio pointed at a directory that no longer exists.
        if was_active:
            remaining = [
                m for m in root.glob("*/*/storyboard_manifest.json")
                if m.parent.name not in _PROTECTED_DIRS
            ]
            if remaining:
                set_active_manifest_path(str(remaining[0].resolve()))
            else:
                config.set_active_manifest(root / "storyboard_manifest.json")

        return {
            "ok": True,
            "deleted": target.name,
            "purged": purge,
            "moved_to": moved_to,
            "bytes": size,
            "was_active": was_active,
        }
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
        # Creating a project makes it active, which repoints the same globals
        # /api/project/select is guarded against repointing.
        refuse_if_jobs_running("creating a project")
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


def _takes(sb) -> int:
    """Draft takes per beat for this episode.

    Was the literal 3 at five call sites, which is why the budget plan's take
    allocation needed somewhere single to land. Clamped so a bad manifest value
    cannot bill for fifty images a beat.
    """
    try:
        return max(1, min(8, int(getattr(sb.render, "variations", 3) or 3)))
    except (TypeError, ValueError):
        return 3


# --- Director spikes (A + B) ---------------------------------------------------
#
# These run server-side because that is where the API keys, the ML dependencies,
# ffmpeg and the project data on /gcs all are. Nothing here writes the manifest.

_RESET_SCOPES = ("stills", "video", "narration", "sfx", "renders", "director")


@app.post("/api/project/reset")
async def reset_project_assets(request: Request):
    """Clear generated media for the active project, keeping the script.

    Body: {"scopes": ["stills","video"], "confirm": "<project title>",
           "dry_run": true}

    Moves media to ``_trash/<project>__reset_<n>/`` rather than unlinking, for
    the same reason project deletion does: a mistake here costs whatever those
    assets cost to generate, and every one of them is paid for.

    Scopes are explicit and additive. There is no "everything" shorthand, because
    the difference between clearing stills and clearing paid video is the
    difference between five dollars and fifty, and a single flag that does both
    invites the expensive mistake.

    The script, the beats, their narration TEXT and the manifest structure are
    never touched — only generated artefacts and the manifest fields that point
    at them.
    """
    try:
        data = await request.json()
        scopes = [s for s in (data.get("scopes") or []) if s in _RESET_SCOPES]
        unknown = set(data.get("scopes") or []) - set(_RESET_SCOPES)
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"unknown scope(s): {sorted(unknown)}; "
                                       f"valid: {list(_RESET_SCOPES)}")
        if not scopes:
            raise HTTPException(status_code=400,
                                detail=f"scopes[] is required; valid: {list(_RESET_SCOPES)}")

        sb = get_current_project()
        dry = bool(data.get("dry_run", False))
        confirm = (data.get("confirm") or "").strip().lower()
        if not dry and confirm not in {(sb.title or "").strip().lower(),
                                       config.project_dir().name.lower()}:
            raise HTTPException(
                status_code=400,
                detail=f"Type the project name to confirm. Expected {sb.title!r}.")

        ep = config.episode_paths(sb.title)
        project_dir = config.project_dir()
        targets: list[Path] = []
        if "stills" in scopes:
            targets.append(config.assets_dir())
        if "renders" in scopes or "video" in scopes:
            targets.append(ep["render"])
        if "narration" in scopes:
            targets.append(ep["narration"])
        if "sfx" in scopes:
            targets.append(ep["sfx"])
        if "director" in scopes:
            targets.append(project_dir / "director")

        counted = {}
        for t in targets:
            try:
                counted[t.name] = sum(1 for _ in t.rglob("*") if _.is_file()) if t.is_dir() else 0
            except OSError:
                counted[t.name] = 0

        if dry:
            return {"ok": True, "dry_run": True, "scopes": scopes,
                    "would_move": {str(t): counted.get(t.name, 0) for t in targets},
                    "note": "Re-send with confirm=<project title> to perform it."}

        trash = project_dir.parent / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = len(list(trash.glob(f"{project_dir.name}__reset_*")))
        bucket = trash / f"{project_dir.name}__reset_{stamp}"
        bucket.mkdir(parents=True, exist_ok=True)

        moved: list[str] = []
        failed: list[str] = []
        for t in targets:
            if not t.is_dir():
                continue
            try:
                _move_tree(t, bucket / t.name)
                moved.append(str(t))
            except Exception as exc:  # noqa: BLE001
                # log_job("reset", ...) dropped this line entirely -- no job named
                # "reset" is ever registered -- so the move failed, nothing was
                # moved, and the response still reported moved_to. Collect the
                # failures and return them.
                failed.append(f"{t.name}: {exc}")

        # Clear the manifest fields that point at what just moved, so the
        # storyboard does not reference media that is no longer there.
        for shot in sb.shots:
            if "stills" in scopes:
                shot.draft_variations = []
                shot.draft_image = None
                shot.chosen_variation = None
            if "video" in scopes:
                shot.video_variations = []
                shot.video_clip = None
                shot.hero_clip = False
        if "video" in scopes or "stills" in scopes:
            # Approval allocated budget against assets that no longer exist.
            sb.storyboard_approved = False
        save_current_project(sb)

        # Report what was verified, not what was attempted. This returned
        # moved_to unconditionally, so a reset that moved nothing read as success.
        return {"ok": not failed, "scopes": scopes,
                "moved_to": str(bucket) if moved else None,
                "moved": moved, "failed": failed, "files": counted,
                "storyboard_approved": sb.storyboard_approved,
                "note": "Media was moved to _trash, not deleted. The script, beats "
                        "and narration text are untouched."}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/characters")
def get_characters():
    """The active project's character sheet, with anchor status.

    `_load_character_anchors` silently drops characters whose structural_anchor is
    empty, so a sheet full of names with no anchors behaves exactly like no sheet
    at all — character anchoring has been inert on every project for that reason.
    `has_anchor` makes that visible instead of leaving it to be discovered.
    """
    chars = characters.load_characters()
    return {
        "ok": True,
        "path": str(config.characters_config()),
        "characters": {
            name: {**spec,
                   "has_anchor": bool((spec.get("structural_anchor") or "").strip())}
            for name, spec in (chars or {}).items()
        },
    }


@app.post("/api/characters/{name}/reference")
async def upload_character_reference(name: str, file: UploadFile = File(...)):
    """Attach a likeness reference to a character.

    Distinct from render.reference_image, which is a FRAME reference -- that path
    instructs the model to keep a page border and replace the interior, the
    opposite of what a likeness needs. This one is the subject: keep the person,
    change the scene.

    Spike A on anchor text alone returned four different men per framing, none of
    them the documented likeness. A 130-word structural description lost to three
    words of setting. Text cannot carry a specific real face.
    """
    try:
        chars = characters.load_characters()
        if name not in chars:
            chars[name] = {}
        ref_dir = config.project_dir() / "references" / "characters"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "ref.png").suffix.lower() or ".png"
        dest = ref_dir / f"{secure_filename(name)}{ext}"
        with open(dest, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        chars[name]["reference_image"] = config.rel_media_path(dest)
        characters.save_characters(chars)
        return {"ok": True, "name": name,
                "reference_image": chars[name]["reference_image"],
                "path": str(dest)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/characters/{name}")
async def put_character(name: str, request: Request):
    """Create or update one character, including its structural anchor."""
    try:
        data = await request.json()
        chars = characters.load_characters()
        spec = dict(chars.get(name) or {})
        for key in ("description", "structural_anchor", "reference_image",
                    "wardrobe", "notes"):
            if key in data:
                spec[key] = data[key]
        chars[name] = spec
        characters.save_characters(chars)
        return {"ok": True, "name": name, "character": spec,
                "has_anchor": bool((spec.get("structural_anchor") or "").strip()),
                "written": str(config.characters_config())}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


# --- Narrator casting ----------------------------------------------------------

@app.post("/api/casting/audition")
async def run_casting_audition(request: Request):
    """Generate the audition set. Selects nothing and saves no voice.

    Body (all optional): {"library_limit": 8, "passage": "...",
                          "include_designed": true}
    """
    try:
        sb = get_current_project()
        data = {}
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — an empty body is fine
            pass

        def fn():
            log = lambda m: log_job("casting", m)  # noqa: E731
            m = casting.run_audition(
                sb,
                library_limit=int(data.get("library_limit") or 8),
                passage=data.get("passage") or "",
                include_designed=bool(data.get("include_designed", True)),
                log=log,
            )
            log(f"{len(m['candidates'])} candidate(s) ready to listen to. "
                f"Nothing was selected or saved.")

        if not start_job("casting", fn):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "an audition is already running"})
        return {"ok": True, "started": True, "job": "casting",
                "project": sb.title}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/casting/audition")
def get_casting_audition():
    """The audition manifest, with playable urls for each candidate."""
    p = casting.auditions_dir() / "audition_manifest.json"
    if not p.is_file():
        return {"ok": True, "manifest": None}
    m = json.loads(p.read_text(encoding="utf-8"))
    base = config.rel_media_path(casting.auditions_dir())
    for c in m.get("candidates") or []:
        c["url"] = f"{base}/{c['audio']}"
    return {"ok": True, "manifest": m}


@app.get("/api/casting/profiles")
def get_vo_profiles():
    """Narrator profiles, plus what this episode currently resolves to."""
    sb = get_current_project()
    profiles = casting.load_profiles()
    active = casting.resolve_profile(sb)
    return {
        "ok": True,
        "profiles": {k: asdict(v) for k, v in profiles.items()},
        "episode_profile": getattr(sb, "vo_profile", "") or "",
        "resolved": asdict(active) if active else None,
        "current_behaviour": {
            "voice_id": (getattr(sb, "voice_id", "") or "").strip()
                        or config.VESPER_VOICE_ID or config.ELEVENLABS_VOICE_ID,
            "model_id": config.ELEVENLABS_MODEL,
            "settings": {
                "stability": config.ELEVENLABS_STABILITY,
                "similarity_boost": config.ELEVENLABS_SIMILARITY_BOOST,
                "style": config.ELEVENLABS_STYLE_EXAGGERATION,
                "use_speaker_boost": config.ELEVENLABS_SPEAKER_BOOST,
            },
            "note": "In effect while no vo_profile is set on the episode.",
        },
    }


@app.post("/api/casting/assign")
async def assign_vo_profile(request: Request):
    """Point this episode at a narrator profile. {"profile": "id"} — "" to clear.

    Per-episode rather than global: different stories want different narrators,
    and the Bestiary's existing voice must not be replaced by a decision made for
    a docudrama. Clearing restores the episode's previous behaviour exactly.
    """
    try:
        data = await request.json()
        key = (data.get("profile") or "").strip()
        if key and key not in casting.load_profiles():
            raise HTTPException(status_code=400,
                                detail=f"no such VO profile: {key!r}")
        sb = get_current_project()
        sb.vo_profile = key
        save_current_project(sb)
        voice, model_id, _ = audio.resolve_voice(sb)
        return {"ok": True, "project": sb.title, "vo_profile": key,
                "resolves_to": {"voice_id": voice, "model_id": model_id},
                "note": "Existing narration files are not regenerated. Delete them "
                        "to re-narrate with the new voice."}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/casting/promote")
async def promote_designed_voice(request: Request):
    """Turn a designed preview into a usable voice, then optionally a profile.

    A preview's generated_voice_id cannot narrate anything -- this is the step
    that makes a designed candidate real, and it only happens after a human has
    chosen. {"generated_voice_id": "...", "name": "...", "profile_id": "..."}
    """
    try:
        data = await request.json()
        gen_id = (data.get("generated_voice_id") or "").strip()
        name = (data.get("name") or "").strip()
        if not gen_id or not name:
            raise HTTPException(status_code=400,
                                detail="generated_voice_id and name are required")
        saved = audio.save_designed_voice(name, data.get("description") or name, gen_id)
        out = {"ok": True, "voice": saved}
        pid = (data.get("profile_id") or "").strip()
        if pid:
            prof = casting.VOProfile(id=pid, name=name,
                                     voice_id=saved.get("voice_id", ""))
            casting.save_profile(prof)
            out["profile"] = asdict(prof)
        return out
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/casting/profiles")
async def put_vo_profile(request: Request):
    """Create or update a narrator profile. Does not assign it to any episode."""
    try:
        data = await request.json()
        if not (data.get("id") or "").strip():
            raise HTTPException(status_code=400, detail="id is required")
        prof = casting.VOProfile(**{k: v for k, v in data.items()
                                    if k in casting.VOProfile.__dataclass_fields__})
        path = casting.save_profile(prof)
        return {"ok": True, "profile": asdict(prof), "written": str(path)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/director/profiles")
def get_director_profiles():
    """Director profiles and the model capability table. Both are data, not code."""
    return {
        "ok": True,
        "profiles": planner.DIRECTOR_PROFILES,
        "default_profile": planner.DEFAULT_PROFILE,
        "vocabulary": {
            "purpose": planner.PURPOSES, "shot_size": planner.SHOT_SIZES,
            "angle": planner.ANGLES, "camera_move": planner.MOVES,
            "motion_type": planner.MOTION_TYPES,
        },
        "video_capabilities": capabilities.table(),
    }


@app.get("/api/director/survey")
def get_director_survey(profile: str = ""):
    """Which beats are worth covering, with the cost of not covering them.

    Answers the question that comes before planning. Free, instant, and
    arithmetic — no model is asked to rank beats, because a plausible ordering
    nobody can check is worse than none.
    """
    sb = get_current_project()
    return {"ok": True, **planner.survey(sb, profile or None)}


def _replan_notes(base: str, warnings: list[dict]) -> str:
    """Fold the critic's warnings into notes for the next planning round."""
    lines = [base.strip()] if base.strip() else []
    lines.append("The previous version of this coverage drew the criticism below. "
                 "Address each point; do not simply restate the same plan.")
    for w in warnings:
        where = w.get("shot_id") or w.get("beat_id") or "scene"
        detail = (w.get("detail") or "").strip()
        fix = (w.get("suggestion") or "").strip()
        lines.append(f"- [{where}] {w.get('kind', 'note')}: {detail}"
                     + (f" Suggested fix: {fix}" if fix else ""))
    return "\n".join(lines)


@app.post("/api/director/plan")
async def plan_director_scene(request: Request):
    """Plan coverage for a scene. Writes drafts; generates nothing.

    Body: {"beats": ["s004","s005"], "profile": "historical_docudrama",
           "notes": "less dramatic, more environment", "critique": true,
           "rounds": 2, "replan": false}

    ``rounds`` runs plan -> critic -> re-plan against the criticism -> critic,
    up to 3 times, keeping whichever round the critic liked best and stopping
    early once it raises nothing. Text only -- no image or video is generated at
    any round, so refinement is cheap here and expensive after generation.
    """
    try:
        sb = get_current_project()
        data = await request.json()
        beat_ids = [str(b) for b in (data.get("beats") or []) if b]
        if not beat_ids:
            raise HTTPException(status_code=400, detail="beats[] is required")
        known = {s.scene_id for s in sb.shots}
        missing = [b for b in beat_ids if b not in known]
        if missing:
            raise HTTPException(status_code=400, detail=f"unknown beats: {missing}")

        profile_key = data.get("profile")
        notes = data.get("notes") or ""
        run_critic = data.get("critique", True)
        replan = bool(data.get("replan"))
        # plan -> critic -> re-plan against the criticism -> critic again, in one
        # job. Every round is text only: nothing is generated and nothing is
        # billed beyond the LLM calls, so refining before a human looks is cheap
        # in exactly the place where changing your mind later is not.
        rounds = max(1, min(3, int(data.get("rounds") or 1)))
        if rounds > 1 and not run_critic:
            raise HTTPException(status_code=400,
                                detail="rounds > 1 requires critique=true — "
                                       "there is nothing to re-plan against.")
        job = f"director_plan:{beat_ids[0]}"

        def fn():
            log = lambda m: log_job(job, m)  # noqa: E731
            protected: set[str] = set()
            warnings: list[dict] = []
            carry = notes
            best: tuple[int, dict] | None = None   # (warning count, plans on disk)
            history: list[int] = []

            for rnd in range(1, rounds + 1):
                if rounds > 1:
                    log(f"--- round {rnd} of {rounds} ---")
                result = planner.plan_scene(sb, beat_ids, profile_key, carry,
                                            log=log, replan=replan)
                # Never write over a plan we deliberately declined to re-plan --
                # including its warnings.
                protected = set(result.get("skipped") or [])

                # The critic is advisory. It runs after the plans are already
                # written, so letting it fail the job threw away 23 good shots
                # over a warning pass -- the plans were on disk and correct, and
                # the run still reported error.
                warnings = []
                if run_critic:
                    try:
                        warnings = planner.critique(sb, beat_ids, log=log)
                    except Exception as exc:  # noqa: BLE001
                        log(f"  !! critique failed ({exc}) — plans are saved; "
                            f"re-run POST /api/director/critique to retry")

                # Warnings belong on the beat they concern, so the studio can
                # show them beside the shot rather than in a separate list.
                for bid in beat_ids:
                    if bid in protected:
                        continue
                    pl = director.load_plan(bid)
                    if not pl:
                        continue
                    pl.warnings = [w for w in warnings
                                   if w.get("beat_id") in ("", bid)]
                    director.save_plan(pl)

                history.append(len(warnings))
                # Keep this round only if it is actually an improvement. A
                # re-plan can fix the shot it was told about and break two
                # others; without this the loop would end on whichever round
                # happened to run last and report it as refined.
                snapshot = {bid: director.load_plan(bid) for bid in beat_ids
                            if bid not in protected}
                if best is None or len(warnings) < best[0]:
                    best = (len(warnings), snapshot)

                if not warnings:
                    log(f"  round {rnd}: the critic raised nothing — stopping here.")
                    break
                if rnd < rounds:
                    carry = _replan_notes(notes, warnings)

            if best is not None and history and history[-1] > best[0]:
                log(f"  the last round scored worse ({history[-1]} warnings vs "
                    f"{best[0]}) — restoring the better plan.")
                for pl in best[1].values():
                    if pl:
                        director.save_plan(pl)

            msg = f"Planned {len(beat_ids) - len(protected)} beat(s); nothing was generated."
            if len(history) > 1:
                msg += f" Warnings by round: {' -> '.join(str(h) for h in history)}."
            elif history:
                msg += f" {history[0]} warning(s) for review."
            if protected:
                msg += (f" Left {len(protected)} already-covered beat(s) untouched: "
                        f"{', '.join(sorted(protected))}.")
            log(msg)

        if not start_job(job, fn):
            return JSONResponse(status_code=409, content={
                "ok": False, "error": f"a plan for {beat_ids[0]} is already running"})
        return {"ok": True, "started": True, "job": job, "beats": beat_ids,
                "rounds": rounds,
                "profile": planner.profile(profile_key)["key"]}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/director/critique")
async def critique_director_scene(request: Request):
    """Re-run the critic over existing plans. Read-only apart from warnings."""
    try:
        sb = get_current_project()
        data = await request.json()
        beat_ids = [str(b) for b in (data.get("beats") or []) if b]
        if not beat_ids:
            raise HTTPException(status_code=400, detail="beats[] is required")
        warnings = planner.critique(sb, beat_ids)
        for bid in beat_ids:
            p = director.load_plan(bid)
            if p:
                p.warnings = director.normalize_warnings(
                    [w for w in warnings if w.get("beat_id") in ("", bid)])
                # Drop decisions about findings this re-critique no longer
                # reports. Keeping them would let a stale "accepted" silently
                # cover a warning that came back with different wording.
                live = {w["id"] for w in p.warnings}
                p.warning_dispositions = {k: v for k, v in
                                          (p.warning_dispositions or {}).items()
                                          if k in live}
                director.save_plan(p)
        return {"ok": True, "warnings": warnings,
                "summary": planner.scene_summary(beat_ids)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/director/shot/{shot_id}")
async def patch_director_shot(shot_id: str, request: Request):
    """Edit one Director Shot inside its plan. The only sanctioned way to do it.

    Body: any of shot_size, angle, purpose, composition, subject, prompt,
          motion_prompt, camera_move, duration, motion_type, face_visibility,
          gestural, identity_critical

    Duration and motion_type are why this lives on the server. Coverage must sum
    to the beat or the plan cannot compile, so lengthening one shot has to take
    the time from somewhere; and setting motion_type to ai_video is only legal if
    some model can actually produce that length -- generated video comes in fixed
    sizes that differ per endpoint, and a 3.34s paid shot is not slightly wrong,
    it is unproducible. A client writing these fields directly is how that
    happened once already.
    """
    try:
        beat_id = shot_id.split(".")[0]
        plan = director.load_plan(beat_id)
        if not plan:
            raise HTTPException(status_code=404, detail=f"no plan for {beat_id}")
        if plan.status in ("compiling", "compiled"):
            raise HTTPException(status_code=409,
                                detail=f"{beat_id} is {plan.status}; unlock or re-plan first")
        ds = next((s for s in plan.coverage if s.id == shot_id), None)
        if ds is None:
            raise HTTPException(status_code=404, detail=f"no shot {shot_id}")

        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")

        # Accept the nested camera shape the studio was already sending. The
        # drawer posted {"camera": {"duration": 4.2}} while this only ever read a
        # flat "duration", so the slider round-tripped a 200 and changed nothing --
        # the endpoint reported success for an edit it had silently discarded.
        rebalanced: set[str] = set()
        camera_in = data.pop("camera", None)
        if isinstance(camera_in, dict):
            if "duration" in camera_in and "duration" not in data:
                data["duration"] = camera_in["duration"]
            if "move" in camera_in and "camera_move" not in data:
                data["camera_move"] = camera_in["move"]

        # Fields the SERVER owns. A client may echo them back in a round-tripped
        # shot object; they are dropped, not rejected, because the server
        # recomputes each one below and a 400 here would reject an otherwise
        # valid edit.
        #
        # This is the correction to a regression I introduced: rejecting every
        # unrecognised key without first grepping the callers 400'd the Motion
        # Technique buttons (which send estimated_cost beside motion_type) and
        # take selection (chosen_variation). That killed the Tier-C budget
        # control -- the Gate-1 allocator -- in the name of a stricter contract.
        for derived in ("estimated_cost", "backend", "constrained_by", "clip",
                        "error", "id", "beat_id", "reason", "draft_variations"):
            data.pop(derived, None)

        # Reject what is genuinely unknown. Silently ignoring unrecognised keys is
        # what let two separate client bugs report success for edits that never
        # happened, so the check stays -- it just no longer fires on fields the
        # server itself produced.
        known = {"shot_size", "angle", "purpose", "composition", "subject",
                 "prompt", "motion_prompt", "face_visibility", "gestural",
                 "identity_critical", "camera_move", "duration", "motion_type",
                 "chosen_variation"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown field(s) {unknown} — this endpoint accepts "
                       f"{sorted(known)}. Nothing was changed.")

        if "chosen_variation" in data:
            v = data["chosen_variation"]
            if v is not None:
                v = int(v)
                # No max(1, ...) escape hatch: a shot with no drafts has no take
                # 0 either, and accepting one records a choice that indexes a
                # list which does not exist.
                if not (0 <= v < len(ds.draft_variations or [])):
                    raise HTTPException(
                        status_code=400,
                        detail=f"take {v} does not exist for {shot_id} "
                               f"({len(ds.draft_variations or [])} take(s) generated)")
            ds.chosen_variation = v

        for f in ("shot_size", "angle", "purpose", "composition", "subject",
                  "prompt", "motion_prompt", "face_visibility"):
            if f in data:
                setattr(ds, f, str(data[f] or ""))
        for f in ("gestural", "identity_critical"):
            if f in data:
                setattr(ds, f, bool(data[f]))
        if "camera_move" in data:
            ds.camera.move = str(data["camera_move"] or "static")

        notes: list[str] = []

        if "duration" in data:
            want = max(0.5, float(data["duration"]))
            delta = round(want - ds.duration, 2)
            ds.camera.duration = round(want, 2)
            # Take the difference from the other shots, largest first, so the beat
            # still adds up. Without this the edit silently breaks the plan and the
            # user only discovers it when locking fails.
            others = [o for o in plan.coverage if o.id != ds.id]
            remaining = delta
            rebalanced: set[str] = set()
            for o in sorted(others, key=lambda x: -x.duration):
                if abs(remaining) < 0.01:
                    break
                # max(0.0, ...): a sibling already at or below the floor has no
                # room to give. Without the clamp `room` goes negative, `take` is
                # negative, and the sibling is LENGTHENED -- growing the very
                # shortfall this loop is closing.
                room = max(0.0, o.duration - planner.MIN_SHOT_SECONDS) if remaining > 0 else float("inf")
                take = min(remaining, room) if remaining > 0 else remaining
                if abs(take) >= 0.01:
                    rebalanced.add(o.id)
                o.camera.duration = round(o.duration - take, 2)
                remaining = round(remaining - take, 2)
            if abs(remaining) >= 0.01:
                notes.append(f"could not rebalance {remaining:+.2f}s across the other shots")

        def _reprice(target_ds, *, strict: bool):
            """Re-route and re-price one shot for its CURRENT duration and tier.

            Price and model both depend on length, so a duration edit invalidates
            them exactly as a tier edit does. Only the tier branch re-ran this, so
            dragging a shot from 4s to 10s left it priced and routed for 4s -- an
            ai_video shot stayed on wan_2_7 at $0.39 when the router's real answer
            was kling_2_1_standard at $0.65. That is a 66% under-report on the
            screen where the human allocates the Gate-1 budget. The rebalance
            spreads the same staleness onto siblings nobody touched.

            strict=False for siblings: a sibling that becomes unproducible is a
            note, not a 400, because the user did not ask to change it and
            refusing would make the edit they DID ask for impossible.
            """
            if target_ds.motion_type != "ai_video":
                target_ds.backend = ""
                target_ds.estimated_cost = capabilities.COST_PER_IMAGE
                return None
            routed = capabilities.resolve({"duration": target_ds.duration,
                                           "gestural": target_ds.gestural})
            if not routed.get("backend"):
                if strict:
                    return routed
                # Clear the stale routing too. Leaving backend and estimated_cost
                # as they were meant planner.scene_summary went on summing a price
                # for a shot that can no longer be generated at that length, into
                # the total on the screen where the human approves the budget --
                # and director.validate does not check routability, so the plan
                # locked clean and only failed at compile, after approval.
                target_ds.backend = ""
                target_ds.estimated_cost = capabilities.COST_PER_IMAGE
                if "no_legal_backend" not in target_ds.constrained_by:
                    target_ds.constrained_by.append("no_legal_backend")
                notes.append(f"{target_ds.id}: now {target_ds.duration:.2f}s, which "
                             f"no model can generate — change its length or drop it "
                             f"to a free tier before locking")
                return None
            target_ds.backend = routed["backend"]
            target_ds.estimated_cost = round(capabilities.COST_PER_IMAGE
                                             + float(routed.get("estimated_cost") or 0), 3)
            for c in routed.get("constraints") or []:
                if c not in target_ds.constrained_by:
                    target_ds.constrained_by.append(c)
            notes.append(f"{target_ds.id}: routed to {routed['backend']} at "
                         f"{routed['generate_seconds']}s (${target_ds.estimated_cost:.2f})")
            return None

        if "motion_type" in data:
            mt = str(data["motion_type"])
            if mt not in ("static", "parallax", "ai_video"):
                raise HTTPException(status_code=400, detail=f"bad motion_type {mt!r}")
            ds.motion_type = mt

        # Re-price after BOTH kinds of edit, and for every sibling the rebalance
        # moved -- their durations changed, so their prices did too.
        # Strict only when the body actually asked for something that changes
        # routing. Keying on identity alone meant that once a rebalance had left
        # THIS shot at an unroutable length, every later patch to it -- selecting a
        # take, editing a prompt -- 400'd citing a duration the user never typed,
        # which would have killed the Take Selector all over again.
        router_edit = any(k in data for k in ("duration", "motion_type", "gestural"))
        touched = [ds] + [o for o in plan.coverage
                          if o.id != ds.id and o.id in rebalanced]
        for t in touched:
            failed = _reprice(t, strict=(t is ds and router_edit))
            if failed is not None:
                return JSONResponse(status_code=400, content={
                    "ok": False,
                    "error": f"no model can generate {ds.duration:.2f}s"
                             + (" without trimming a gesture" if ds.gestural else ""),
                    "hint": "change the duration, or clear gestural",
                })

        director.save_plan(plan)

        sb = get_current_project()
        beat = next((s for s in sb.shots if s.scene_id == beat_id), None)
        problems = []
        if beat is not None:
            try:
                director.validate(plan, beat)
            except director.PlanError as exc:
                problems.append(str(exc))
        shot_out = asdict(ds)
        # The list endpoint resolves this; the patch response did not, so
        # setSelectedShot(res.shot) replaced a shot that had a thumbnail with one
        # that did not and the image went blank after every edit.
        _vars = ds.draft_variations or []
        _i = ds.chosen_variation
        shot_out["thumbnail_url"] = (
            _vars[_i] if isinstance(_i, int) and 0 <= _i < len(_vars)
            else (_vars[0] if _vars else ""))
        return {"ok": True, "shot": shot_out, "notes": notes,
                "problems": problems,
                "coverage_total": round(plan.total_duration(), 3),
                "beat_duration": float(beat.camera.duration) if beat else None}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/director/warning/{beat_id}/{warning_id}")
async def decide_director_warning(beat_id: str, warning_id: str, request: Request):
    """Record a human decision about one critic warning.

    This exists because the studio had no way to say anything durable about a
    finding: the warning list could be cleared in the browser, which changed
    React state and nothing else, so the screen reported a clean review while the
    persisted plan still carried the warning -- and a refresh brought it back. A
    dismissal the server never hears about is not a decision.

    ``decision`` is "resolved" (the plan was changed to answer the finding) or
    "accepted" (understood and deliberately kept). Sending "" clears it again.
    """
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
    except Exception:  # noqa: BLE001
        data = {}
    plan = director.load_plan(beat_id)
    if not plan:
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": f"no plan for {beat_id}"})
    if plan.status == "compiling":
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": f"{beat_id} is compiling; cannot change its review state"})
    try:
        director.resolve_warning(plan, warning_id,
                                 str(data.get("decision", "resolved") or ""),
                                 note=str(data.get("note") or ""))
    except director.PlanError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    director.save_plan(plan)
    return {"ok": True, "beat_id": beat_id,
            "warnings": plan.warnings,
            "warning_dispositions": plan.warning_dispositions,
            "unresolved": len(director.unresolved_warnings(plan))}


@app.post("/api/director/lock_scene")
async def lock_director_scene(request: Request):
    """Lock every beat in a scene at once. {"beats": [...]}

    Validates all of them first and locks none if any fails — a half-locked scene
    is worse than an unlocked one, because the render path would then skip some
    beats and cover others.
    """
    try:
        data = await request.json()
        ids = [str(b) for b in (data.get("beats") or []) if b]
        if not ids:
            raise HTTPException(status_code=400, detail="beats[] is required")
        sb = get_current_project()
        plans, problems = [], []
        for bid in ids:
            plan = director.load_plan(bid)
            beat = next((x for x in sb.shots if x.scene_id == bid), None)
            if not plan or beat is None:
                problems.append(f"{bid}: no plan")
                continue
            if plan.status == "compiling":
                problems.append(f"{bid}: currently compiling")
                continue
            try:
                director.validate(plan, beat)
            except director.PlanError as exc:
                problems.append(str(exc))
                continue
            # Contract 5.4: a bulk action must not silently approve unresolved
            # critic findings. Validation only checked arithmetic and shape, so
            # "Approve Scene Plan" used to lock straight past every warning the
            # critic had raised -- turning "approved" into a state that says
            # nothing about whether the review was answered.
            undecided = director.unresolved_warnings(plan)
            if undecided:
                ids = ", ".join(w["id"] for w in undecided[:4])
                more = " and more" if len(undecided) > 4 else ""
                problems.append(
                    f"{bid}: {len(undecided)} critic warning(s) awaiting a decision "
                    f"({ids}{more})")
                continue
            plans.append(plan)
        if problems:
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": "nothing was locked",
                                         "problems": problems})
        for plan in plans:
            director.approve(plan)
            plan.status = "locked"
            director.save_plan(plan)
            try:
                ledger.record_plan_outcome(beat_id=plan.beat_id, plan_id=plan.plan_id,
                                           outcome="locked", shots=plan.coverage)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "locked": [p.beat_id for p in plans],
                "estimated_cost": planner.scene_summary(ids)["estimated_cost"]}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/director/scene")
def get_director_scene(beats: str = "", tier: str = ""):
    """Everything the studio needs for a scene: plans, cost, warnings, status.

    `beats` is a comma-separated list, e.g. ?beats=s004,s005
    """
    ids = [b.strip() for b in beats.split(",") if b.strip()]
    if not ids:
        return JSONResponse(status_code=400, content={"ok": False, "error": "beats is required"})
    sb = get_current_project()
    durations = {s.scene_id: float(s.camera.duration) for s in sb.shots}
    out = []
    for bid in ids:
        p = director.load_plan(bid)
        entry = {
            "beat_id": bid,
            "beat_duration": durations.get(bid),
            "plan": asdict(p) if p else None,
            "coverage_total": round(p.total_duration(), 3) if p else 0.0,
        }
        if p:
            # Tier is computed here, never in the client. See director.shot_tier.
            entry["triage"] = director.triage(p)
            by_id = {ds.id: director.shot_tier(ds, p.warnings) for ds in p.coverage}
            for shot in entry["plan"]["coverage"]:
                shot.update(by_id.get(shot["id"], {}))
                # One field for "the image that represents this shot", so the
                # client never has to index draft_variations itself. The frontend
                # was reading draft_variations[chosen_variation - 1], which is off
                # by one (the index is 0-based) and skipped entirely when the
                # chosen take is 0, because 0 is falsy in JS. Both bugs disappear
                # if the server just says which image it is.
                idx = shot.get("chosen_variation")
                variations = shot.get("draft_variations") or []
                pick = ""
                if isinstance(idx, int) and 0 <= idx < len(variations):
                    pick = variations[idx]
                elif variations:
                    pick = variations[0]
                shot["thumbnail_url"] = pick        # "" until stills are generated
            if tier == "needs_review":
                keep = set(entry["triage"]["needs_review"])
                entry["plan"]["coverage"] = [c for c in entry["plan"]["coverage"]
                                             if c["id"] in keep]
        out.append(entry)
    return {"ok": True, "beats": out, "summary": planner.scene_summary(ids),
            "tier": tier or "all"}


@app.post("/api/director/lock/{beat_id}")
def lock_director_plan(beat_id: str, locked: bool = True):
    """Mark a plan locked (or back to draft). Still generates nothing.

    Locking is the human decision that a plan is worth producing. It is what the
    compile endpoint requires, and what makes the beat protected from the ordinary
    render path.
    """
    plan = director.load_plan(beat_id)
    if not plan:
        return JSONResponse(status_code=404, content={"ok": False, "error": f"no plan for {beat_id}"})
    if plan.status == "compiling":
        return JSONResponse(status_code=409, content={
            "ok": False, "error": f"{beat_id} is compiling; cannot change its status"})
    sb = get_current_project()
    beat = next((s for s in sb.shots if s.scene_id == beat_id), None)
    if locked and beat is not None:
        try:
            director.validate(plan, beat)
        except director.PlanError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    if locked:
        # Same rule as the scene-level lock. Unlocking back to draft stays
        # allowed: returning a plan for more work is not an approval.
        undecided = director.unresolved_warnings(plan)
        if undecided:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": f"{beat_id} has {len(undecided)} critic warning(s) awaiting a "
                         f"decision; resolve or accept each one before locking.",
                "warnings": undecided,
            })
    if locked:
        # Bind the approval to the plan as it stands. "locked" alone is a claim
        # that a human acted; the signature is what they acted on.
        director.approve(plan)
    else:
        plan.approved_signature = ""
        plan.approved_at = ""
        plan.approved_by = ""
    plan.status = "locked" if locked else "draft"
    director.save_plan(plan)
    # Locking is the human verdict on the planner's proposal, and the only point
    # where accepted and rejected are distinguishable. Recording it here is what
    # turns prompt tuning from inspection into measurement.
    try:
        ledger.record_plan_outcome(
            beat_id=beat_id, plan_id=plan.plan_id,
            outcome="locked" if locked else "replanned",
            shots=plan.coverage)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "beat_id": beat_id, "status": plan.status}


def _plan_payload(plan) -> dict:
    """Plan as the client sees it, with approval stated rather than inferred.

    The UI must not decide for itself whether a plan is approved by reading
    `status`: that is exactly the inference which stays true after the plan has
    materially changed underneath it.
    """
    from dataclasses import asdict as _asdict
    d = _asdict(plan)
    d["plan_signature"] = director.plan_signature(plan)
    d["approval_is_current"] = director.approval_is_current(plan)
    d["unresolved_warnings"] = len(director.unresolved_warnings(plan))
    return d


@app.get("/api/director/plan/{beat_id}")
def get_director_plan(beat_id: str):
    """The coverage plan for one beat, or null. Never touches the manifest."""
    plan = director.load_plan(beat_id)
    if not plan:
        return {"ok": True, "plan": None}
    sb = get_current_project()
    beat = next((s for s in sb.shots if s.scene_id == beat_id), None)
    problems = []
    if beat:
        try:
            director.validate(plan, beat)
        except director.PlanError as exc:
            problems.append(str(exc))
    return {
        "ok": True,
        "plan": _plan_payload(plan),
        "beat_duration": float(beat.camera.duration) if beat and beat.camera else None,
        "coverage_total": round(plan.total_duration(), 3),
        "problems": problems,
        # Stated, not inferred: a client that decides "approved" for itself by
        # reading status keeps saying yes after the plan has changed underneath.
        "approval_is_current": director.approval_is_current(plan),
        "plan_signature": director.plan_signature(plan),
    }


@app.post("/api/director/plan/{beat_id}")
async def put_director_plan(beat_id: str, request: Request):
    """Write a hand-authored coverage plan (Spike B). Validated, not compiled."""
    try:
        # Sync the active project BEFORE resolving director_dir(). This wrote the
        # plan, then read it back, then called get_current_project() -- so on a
        # stale config the plan landed in the previous project's director/ and the
        # response reported that wrong path as "written". The follow-up compile,
        # which does sync first, then 404s with "no director plan" for a plan the
        # API had just confirmed writing.
        sb = get_current_project()
        data = await request.json()
        data["beat_id"] = beat_id
        path = director.director_dir() / f"{beat_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        plan = director.load_plan(beat_id)
        beat = next((s for s in sb.shots if s.scene_id == beat_id), None)
        problems = []
        if beat:
            try:
                director.validate(plan, beat)
            except director.PlanError as exc:
                problems.append(str(exc))
        return {"ok": True, "written": str(path), "shots": len(plan.coverage),
                "coverage_total": round(plan.total_duration(), 3),
                "problems": problems}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/director/compile/{beat_id}")
def compile_director_coverage(beat_id: str):
    """Render the coverage and assemble the beat clip. Spike B's whole point.

    There is no force flag. There used to be, and ``force=true`` skipped the
    draft check entirely -- so any caller could send an unapproved plan into a
    compile that generates stills and, for ai_video shots, buys paid video.
    Approval is the whole boundary between a proposal and spending money on it,
    and a query parameter that steps over it is not a gate.

    Recovery goes the way a human does: lock the plan, then compile.
    """
    try:
        sb = get_current_project()
        plan = director.load_plan(beat_id)
        if not plan:
            raise HTTPException(status_code=404, detail=f"no director plan for {beat_id}")
        # §11.5: an approved plan must not silently mutate after approval.
        # Drift is detected once, in director.load_plan, which drops the stale
        # approval and returns the plan to draft -- so `status` never claims an
        # approval that no longer exists, and every consumer sees the same
        # truth. That means the check here is the draft check; what it adds is
        # saying WHICH kind of unapproved this is, because "lock it first" is
        # misleading advice for a plan that was locked until someone edited it.
        if plan.status == "draft":
            drifted = next((h for h in reversed(plan.approval_history or [])
                            if h.get("invalidated_because")), None)
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": (f"plan for {beat_id} changed after it was approved, so the "
                          f"approval no longer covers it; review and lock it again."
                          if drifted else
                          f"plan for {beat_id} is a draft; lock it first - approval is "
                          f"what allocates the render budget."),
                "approval_drifted": bool(drifted),
                "plan_signature": director.plan_signature(plan),
            })
        # Defence in depth. A locked plan should never carry an undecided
        # warning, because locking now refuses one; asserting it here too means a
        # plan locked before this rule existed cannot walk into generation.
        undecided = director.unresolved_warnings(plan)
        if undecided:
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": f"{beat_id} has {len(undecided)} critic warning(s) with no "
                         f"recorded decision; resolve or accept them first.",
                "warnings": undecided,
            })
        ep = config.episode_paths(sb.title)
        ep["render"].mkdir(parents=True, exist_ok=True)

        # Keyed per beat, not "director". Three compiles fired together all took
        # the same key, start_job returned False for the second and third because
        # one was already running, this endpoint ignored that and reported
        # {"started": true} anyway — so s011 and s017 were silently dropped while
        # the caller was told they had begun. Per-beat keys also let them run
        # concurrently, which is safe: each compile touches only its own plan
        # file, its own sub-clip directory and its own beat clip, and none of them
        # writes the manifest.
        job = f"director:{beat_id}"

        def fn():
            log = lambda m: log_job(job, m)  # noqa: E731
            log(f"Compiling coverage for {beat_id} ({len(plan.coverage)} shots)...")
            out = director.compile_coverage(plan, sb, ep["render"], log=log)
            log(f"Beat clip written: {_safe_rel_path(out)}")

        if not start_job(job, fn):
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": f"a compile for {beat_id} is already running",
            })
        return {"ok": True, "started": True, "beat_id": beat_id, "job": job,
                "shots": len(plan.coverage)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/spike/identity/run")
async def run_identity_spike(request: Request):
    """Spike A. Progressive: pass `cells` to widen only where results justify it."""
    try:
        data = await request.json()
        cfg = spike_identity.SpikeConfig(
            character=data["character"],
            style_medium=data.get("style_medium") or "",
            setting=data.get("setting") or "",
            backends=data.get("backends") or ["nano2"],
            strategies=data.get("strategies") or [spike_identity.STRATEGIES[0]],
            cells=data.get("cells") or ["cu", "mcu", "m", "profile", "ots"],
            takes=int(data.get("takes") or 4),
        )
        if not cfg.style_medium:
            sb = get_current_project()
            first = next((s for s in sb.shots if s.style_medium), None)
            cfg.style_medium = first.style_medium if first else ""

        result: dict = {}

        def fn():
            log = lambda m: log_job("spike_identity", m)  # noqa: E731
            result.update(spike_identity.run(cfg, log=log))
            log(f"Spike A complete: {result.get('cells_run')} cells, "
                f"~${result.get('estimated_spend')}")

        start_job("spike_identity", fn)
        return {"ok": True, "started": True, "cells": cfg.cells, "takes": cfg.takes}
    except KeyError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"missing field: {e}"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.post("/api/spike/identity/score")
async def score_identity_spike(request: Request):
    """Human evaluation of one generated image. 0-3 per score key."""
    try:
        data = await request.json()
        spike_identity.score_cell(
            scene_id=data["scene_id"], path=data["path"],
            scores=data.get("scores") or {}, reason=data.get("reason") or "")
        return {"ok": True}
    except KeyError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"missing field: {e}"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/spike/identity/sheet")
def identity_spike_sheet():
    """Every take, grouped by framing, with urls — the thing you actually look at."""
    return {"ok": True, **spike_identity.contact_sheet()}


@app.post("/api/spike/identity/score_cell")
async def score_identity_cell(request: Request):
    """Score a whole framing at once: {"scene_id": "...", "scores": {...}}

    Judging identity is a per-FRAMING verdict, not a per-image one -- the question
    is whether a close-up of this man is reliable, and answering it four times per
    cell is data entry that nobody finishes.
    """
    try:
        data = await request.json()
        sid = (data.get("scene_id") or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="scene_id is required")
        rows = [r for r in ledger.read_rows()
                if r.get("event") == "generate" and r.get("scene_id") == sid]
        if not rows:
            raise HTTPException(status_code=404, detail=f"no takes recorded for {sid}")
        for r in rows:
            spike_identity.score_cell(sid, r["path"], data.get("scores") or {},
                                      data.get("reason") or "")
        return {"ok": True, "scene_id": sid, "scored": len(rows)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/spike/identity/report")
def identity_spike_report():
    """Coverage vocabulary: which framings can carry the face."""
    return spike_identity.report()


@app.get("/api/script/budget_plan")
def get_budget_plan(budget: float | None = None, beats: int | None = None):
    """What a given budget buys, before drafting anything.

    The same function the script stage uses, so the number shown here is the number
    the model is actually told — not a second estimate that can drift from it.
    """
    plan = script.plan_for_budget(budget, beats)
    return {"ok": True, "plan": plan,
            "note": None if plan else "No budget given — default scope (15-40 beats, ai_video used sparingly)."}


@app.get("/api/director/planner_report")
def get_planner_report(scope: str = "all"):
    """Which planner tendencies survive review, per attribute.

    Deliberately not an overall score. "The planner is 82% good" cannot be acted
    on; "every ecu it proposes gets edited" is a prompt change.
    """
    project = config.project_dir().name if scope == "project" else None
    return {"ok": True, **ledger.planner_report(project=project)}


@app.get("/api/prompts/ledger")
def get_prompt_ledger(scope: str = "all", exemplars: int = 10):
    """Which prompt strategy you actually keep, measured on your own picks.

    ``scope=project`` narrows to the active episode; the default is every episode,
    because learning that does not accumulate across episodes is not learning.
    """
    project = config.project_dir().name if scope == "project" else None
    # Failures belong beside the successes. Reading "generated: 78" and nothing
    # else is how a run that produced one take on 19 of 25 beats read as healthy.
    fails = ledger.failures(project=project)
    by_strategy: dict[str, int] = {}
    by_backend: dict[str, int] = {}
    for r in fails:
        by_strategy[r.get("strategy") or "?"] = by_strategy.get(r.get("strategy") or "?", 0) + 1
        by_backend[r.get("backend") or "?"] = by_backend.get(r.get("backend") or "?", 0) + 1
    return {
        "ok": True,
        "strategies_available": list(assets.PROMPT_STRATEGIES),
        "variants_enabled": assets.PROMPT_VARIANTS,
        **ledger.summary(project=project),
        "failed": len(fails),
        "failed_by_strategy": by_strategy,
        "failed_by_backend": by_backend,
        "recent_failures": [
            {k: r.get(k) for k in ("scene_id", "strategy", "backend", "slot", "error")}
            for r in fails[-10:]
        ],
        "exemplars": ledger.top_exemplars(limit=max(0, min(exemplars, 50)), project=project),
    }


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
        for key in ("mute_narration", "mute_sfx", "mute_music"):
            if key in data and data[key] is not None:
                setattr(sb.mix, key, bool(data[key]))
        if "solo" in data:
            s = str(data["solo"] or "").strip().lower()
            if s and s not in ("narration", "sfx", "music"):
                raise HTTPException(status_code=400, detail=f"Unknown solo bus: {s}")
            sb.mix.solo = s
        save_current_project(sb)
        return {"ok": True, "mix": asdict(sb.mix)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/render/reference")
async def set_global_reference(file: UploadFile = File(...)):
    try:
        sb = get_current_project()
        config.require_for("assets")
        
        config.references_dir().mkdir(parents=True, exist_ok=True)
        fname = secure_filename(f"global_ref_{file.filename}")
        dest = config.references_dir() / fname
        
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
                # The only place a *human* preference over drafts is expressed.
                # generate_for_shot also sets chosen_variation, but as a display
                # placeholder — scoring that would measure slot order, not taste.
                ledger.record_choice(
                    scene_id=scene_id, path=shot.draft_variations[idx], source="human")
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
        for k, lo, hi in (("offset_narration", -120.0, 120.0),
                          ("fade_in_narration", 0.0, 30.0),
                          ("fade_out_narration", 0.0, 30.0)):
            if k in data and data[k] is not None:
                setattr(shot, k, max(lo, min(hi, float(data[k]))))
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
        config.references_dir().mkdir(parents=True, exist_ok=True)
        dest = config.references_dir() / fname
        
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
            
        dest_dir = config.assets_dir() / scene_id
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
            dest = config.assets_dir() / scene_id / f"edit_{ts}_{i}.png"
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
                [ffmpeg_bin(), "-y", "-v", "error", "-i", str(tmp),
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


def _move_tree(src: Path, dst: Path) -> None:
    """Move a directory on the gcsfuse mount, by hand.

    shutil.move cannot do this. os.rename is unsupported for directories on the
    mount, so shutil falls back to copytree -- and copytree calls
    ``copystat(src, dst)`` on every DIRECTORY unconditionally, at
    shutil._copytree's last line, regardless of copy_function. gcsfuse rejects the
    chmod/utime that implies with "[Errno 1] Operation not permitted". copytree
    then raises before rmtree(src) runs, so the caller saw a 500, the project was
    still there, and a full byte-for-byte duplicate was left in _trash -- which
    _scan_projects hides. Every retry doubled the storage silently.

    Passing copy_function=shutil.copyfile does NOT fix that: copy_function governs
    file copies only. I made exactly that mistake, and the review caught that the
    behaviour was identical to no fix at all. So this walks the tree itself and
    touches no metadata anywhere.

    The source is removed only after the copy is verified file-for-file, because
    the failure this replaces was one where a half-finished move reported success.
    """
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    for item in sorted(src.rglob("*")):
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)     # bytes only; never copy2/copystat
            copied += 1

    expected = sum(1 for f in src.rglob("*") if f.is_file())
    landed = sum(1 for f in dst.rglob("*") if f.is_file())
    if landed < expected:
        raise RuntimeError(
            f"refusing to delete {src.name}: copied {landed} of {expected} file(s) "
            f"to {dst}. The source is untouched."
        )
    shutil.rmtree(src)


def refuse_if_jobs_running(what: str) -> None:
    """Refuse an operation that repoints the process config while a job is live.

    config.MANIFEST_PATH / ASSETS / REFERENCES_DIR are process globals that worker
    threads read at call time, on a single Cloud Run instance. Repointing them
    mid-render means save_shot_assets can no longer find the beat's scene_id in
    the now-active manifest, returns early, and the paid clip ends up recorded in
    NO manifest at all -- so the next non-forced render bills for it again.

    This guard was added to /api/project/select and only there. /api/project/new
    and the was_active branch of /api/project/delete rebind the same globals and
    had no check, which is what comes of attaching a guard to one route instead of
    to the thing it protects.

    Since jobs began capturing their ProjectContext at enqueue time
    (``pipeline_worker.start_job``), a running job no longer follows the process
    globals, so this should now be belt-and-braces rather than the actual
    defence. It is deliberately kept until that isolation has been independently
    reviewed -- removing the old guard in the same change that replaces it would
    leave nothing to catch a mistake in the replacement.
    """
    running = [k for k, v in get_jobs_status().items() if v.get("status") == "running"]
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"{', '.join(running)} still running — {what} now would redirect "
                   f"its output into another project. Wait for it to finish.")


def require_paid_gate(sb, what: str = "render") -> None:
    """Refuse a paid call until the storyboard is approved.

    CLAUDE.md makes Gate 1 non-bypassable: approval is where the human allocates
    the render budget, so nothing paid may run before it. The check existed in
    three handlers and was missing from the fourth -- which is the failure mode of
    attaching a gate to routes rather than to the spend. One helper, so a new route
    cannot regress it by omission.
    """
    if not getattr(sb, "storyboard_approved", False):
        raise HTTPException(
            status_code=400,
            detail=f"Approve the storyboard first — that is where the {what} "
                   f"budget is allocated.",
        )


@app.post("/api/shot/{scene_id}/generate_video")
async def generate_shot_video(scene_id: str, request: Request):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")

        # Gate 1, before anything spends. This route reached fal with no approval
        # check at all -- the batch render, the rough cut and director coverage
        # each carry one, and this fourth path to Tier C simply never got it. It
        # also buys a $0.15 still on the way down, so the check has to come before
        # the auto-draft below, not merely before the video call.
        require_paid_gate(sb, "render")
        config.require_for("video")
        data = await request.json()
        video_model_key = data.get("video_model") or getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
        shot.video_model = video_model_key
        shot.motion_type = MotionType.AI_VIDEO

        model_endpoint = resolve_video_model_endpoint(video_model_key)
        
        local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
        if not local_image_path or not local_image_path.exists():
            print("Auto-generating still drafts before video render...")
            assets.generate_for_shot(shot, n=_takes(sb), backend=sb.render.backend, render=sb.render)
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

        gen_audio = shot.video_audio
        if gen_audio is None:
            gen_audio = getattr(sb.render, "video_audio", True)

        # Same schema-derived router as the batch path. These two blocks were
        # byte-for-byte duplicates, which is why both carried all four defects.
        dur_args, dur_note = capabilities.video_arguments(
            video_model_key, target_dur, generate_audio=bool(gen_audio),
            cap_to_ceiling=True)
        if dur_note:
            print(f"{scene_id}: {dur_note}")
        dur_int = capabilities.clamp_duration(video_model_key, target_dur)

        motion_prompt = shot.motion_prompt or f"Cinematic motion, high-quality, authentic detail, {shot.prompt}"
        if f"{dur_int}s" not in motion_prompt and "second" not in motion_prompt:
            motion_prompt = f"{motion_prompt} (duration: ~{dur_int} seconds)"

        arguments = {
            "image_url": public_image_url,
            "prompt": motion_prompt,
            **dur_args,
        }

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
        shot_assets_dir = config.assets_dir() / scene_id
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

        # Record the paid clip BEFORE trying to place it in the cut. Placement
        # was failing on the GCS mount and taking the whole request down with it,
        # which left a generation you had already been billed for downloaded to
        # disk but absent from the manifest -- invisible, and regenerating it
        # meant paying twice.
        save_current_project(sb)

        placed = True
        try:
            set_active_video_clip(sb, shot, video_rel_path, out_dir)
            save_current_project(sb)
        except Exception as exc:  # noqa: BLE001 — the clip itself is safe
            placed = False
            log_job("render", f"  !! {scene_id}: clip generated and saved, but could not be "
                              f"placed in the cut: {exc}")

        return {
            "ok": True,
            "video_path": f"/render/{scene_id}.mp4" if placed else None,
            "video_variation": video_rel_path,
            "placed": placed,
            "video_model": video_model_key,
            "warning": None if placed else
                "The clip was generated and kept, but placing it in the cut failed. "
                "Pick it from this beat's video variations, or re-run the render.",
        }
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

        assets.generate_for_shot(shot, n=_takes(sb), backend=backend, render=sb.render)
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
            abs_path.unlink(missing_ok=True)

        # Repointing shot.video_clip was not enough: render/<scene>.mp4 still held
        # the DELETED take's pixels, so preview, FCPXML and master all kept playing
        # the take that was just rejected. Re-rendering did not help either -- the
        # re-bill guard sees a placed file and logs "paid clip already rendered --
        # keeping it" -- so the only escapes were re-billing with force_paid or
        # manually re-picking a variation.
        replaced = None
        if shot.video_clip == rel_path:
            out_dir = config.episode_paths(sb.title)["render"]
            if video_vars:
                set_active_video_clip(sb, shot, video_vars[0], out_dir)
                replaced = video_vars[0]
            else:
                shot.video_clip = None
                (out_dir / f"{scene_id}.mp4").unlink(missing_ok=True)

        save_current_project(sb)
        return {"ok": True, "deleted": rel_path, "now_active": replaced,
                "remaining": len(video_vars)}
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
        budget = data.get("budget")
        channel = data.get("channel") or sb.channel or "bestiary"

        if not topic:
            raise HTTPException(status_code=400, detail="Topic is required")

        def run_draft():
            log_job("script_draft", f"Generating AI script for topic: '{topic}'...")
            new_sb = script.generate_script(topic, num_beats=beats, channel=channel, budget=budget)
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
        budget = data.get("budget")
        channel = data.get("channel") or sb.channel or "bestiary"

        def run_chat_draft():
            log_job("script_draft", "Generating AI script from chat conversation...")
            new_sb = script.generate_script_from_messages(
                messages, num_beats=beats, channel=channel, budget=budget)
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


@app.get("/api/roughcut/plan")
def roughcut_plan():
    """What the rough cut would do right now, step by step.

    The pipeline has a fixed dependency order and the UI made you discover it by
    moving between tabs. This states it plainly: what is done, what runs next,
    and what is blocking.
    """
    try:
        sb = get_current_project()
        ep = config.episode_paths(sb.title)

        def _stems(d):
            try:
                return {p.stem for p in d.iterdir() if p.is_file()}
            except OSError:
                return set()

        # A preview that predates its own inputs is not "done", it is stale.
        # This used to consider only the beat clips, so a preview built before
        # narration was recorded reported "Build preview ✓" with no warning and
        # played with no voice over at all — the audio is as much an input as the
        # video, and a check that cannot see it cannot answer the question.
        def _newest(d, *exts) -> float:
            try:
                times = [f.stat().st_mtime for f in d.iterdir()
                         if f.is_file() and f.stem != "_preview"
                         and (not exts or f.suffix.lower() in exts)]
                return max(times) if times else 0.0
            except OSError:
                return 0.0

        newest_clip = _newest(ep["render"], ".mp4")
        newest_media = max(newest_clip,
                           _newest(ep["narration"]),
                           _newest(ep["sfx"]))

        def _is_current(path: Path, after: float) -> bool:
            try:
                return path.is_file() and path.stat().st_mtime >= after
            except OSError:
                return False

        stills = sum(1 for s in sb.shots if s.draft_image)
        narr = len(_stems(ep["narration"]) & {s.scene_id for s in sb.shots})
        rendered = len(_stems(ep["render"]) & {s.scene_id for s in sb.shots})
        n = len(sb.shots)
        slug = ep["slug"]

        steps = [
            {"key": "drafts", "label": "Draft stills",
             "done": n > 0 and stills >= n, "detail": f"{stills}/{n}",
             "blocked": None if n else "This project has no beats yet."},
            {"key": "approve", "label": "Approve storyboard",
             "done": bool(sb.storyboard_approved), "detail": "",
             "blocked": None if stills >= n and n else "Every beat needs a still first.",
             "manual": True},
            {"key": "narration", "label": "Record narration",
             "done": n > 0 and narr >= n, "detail": f"{narr}/{n}",
             "blocked": None if sb.script_locked or sb.storyboard_approved
                        else "Lock the script first."},
            {"key": "render", "label": "Render beats",
             "done": n > 0 and rendered >= n, "detail": f"{rendered}/{n}",
             "blocked": None if sb.storyboard_approved else "Approve the storyboard first."},
            {"key": "preview", "label": "Build preview",
             "done": _is_current(ep["render"] / "_preview.mp4", newest_media),
             "detail": "" if _is_current(ep["render"] / "_preview.mp4", newest_media)
                       else ("out of date" if (ep["render"] / "_preview.mp4").is_file() else ""),
             "blocked": None if rendered else "Render the beats first."},
            {"key": "timeline", "label": "Export timeline",
             "done": _is_current(config.project_dir() / f"{slug}.fcpxml", newest_media),
             "detail": "" if _is_current(config.project_dir() / f"{slug}.fcpxml", newest_media)
                       else ("out of date" if (config.project_dir() / f"{slug}.fcpxml").is_file() else ""),
             "blocked": None if rendered else "Render the beats first."},
        ]
        nxt = next((s for s in steps if not s["done"]), None)

        # A run can finish every step and still be wrong. The clearest tell is
        # every beat sitting on the Camera default: that means narration timing
        # never reached the manifest, so the cut is uniform slots with the voice
        # overrunning each one. Reporting "complete" for that is worse than
        # reporting a failure.
        warnings = []
        durs = [float(s.camera.duration) for s in sb.shots if s.camera]
        # Only a problem when narration EXISTS and the durations still did not
        # follow it — that is the lost-update race, and it is worth shouting about.
        # A project that simply has not recorded narration yet is at 6.0s because
        # 6.0s is the default, which is the normal starting state and not a fault.
        # Warning about it there tells a brand-new project that something went
        # wrong, and advises re-running a rough cut that would change nothing.
        if narr > 0 and n >= 3 and durs and all(abs(d - 6.0) < 0.01 for d in durs):
            warnings.append(
                f"All {n} beats are still at the 6.0s default even though narration "
                f"exists, so the timing was never applied — a long job probably wrote "
                f"a stale storyboard back over it. Re-run the rough cut to refit them."
            )
        if narr >= n and n and not sb.script_locked:
            warnings.append(
                "Narration exists but the script is unlocked — a long job probably "
                "wrote a stale storyboard back over it."
            )

        # An mtime check cannot see a preview built from *different durations* —
        # the file can be newer than every input and still be cut to a timeline
        # that no longer exists. The sidecar records the runtime it was built at,
        # so compare that against the manifest and say so in seconds.
        pv = ep["render"] / "_preview.mp4"
        if pv.is_file() and durs:
            want = sum(durs)
            # Measure the FILE, not the sidecar. build_preview writes `runtime`
            # from sum(camera.duration) — its intention — but muxes with
            # `-shortest`, so if the concatenated clips are shorter than the audio
            # mix the output is truncated and the sidecar still claims the full
            # length. Trusting it compares the manifest against itself and always
            # agrees. This is how a 590s preview sat behind a 672s manifest and
            # reported clean.
            try:
                actual = timeline._probe_seconds(pv)
            except Exception:  # noqa: BLE001
                actual = 0.0
            if actual > 0 and abs(actual - want) > 0.5:
                warnings.append(
                    f"The preview is {actual:.0f}s but the cut is {want:.0f}s "
                    f"({abs(actual - want):.0f}s out). Beat clips or durations changed "
                    f"after it was built — re-render the beats, then rebuild the preview."
                )

            # A short video track means the *clips* are stale, not just the mux —
            # rebuilding the preview alone would reproduce the same truncation.
            clip_total = 0.0
            for s in sb.shots:
                c = ep["render"] / f"{s.scene_id}.mp4"
                if c.is_file():
                    try:
                        clip_total += timeline._probe_seconds(c)
                    except Exception:  # noqa: BLE001
                        pass
            if clip_total > 0 and abs(clip_total - want) > 1.0:
                warnings.append(
                    f"The rendered beat clips total {clip_total:.0f}s against a "
                    f"{want:.0f}s cut ({abs(clip_total - want):.0f}s out) — the clips "
                    f"predate the current durations. Re-render the beats; rebuilding "
                    f"the preview alone cannot fix this."
                )

        return {"ok": True, "steps": steps,
                "next": nxt["key"] if nxt else None,
                "complete": nxt is None,
                "blocked_on": nxt["blocked"] if nxt else None,
                "warnings": warnings,
                "needs_human": bool(nxt and nxt.get("manual"))}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/assemble/rough_cut")
def build_rough_cut(force_paid: bool = False):
    """Run everything needed for a rough cut, in dependency order, in one job.

    Each step is skipped when its output already exists, so this is safe to press
    again after a failure or a partial run -- it picks up where it stopped rather
    than regenerating (and re-billing) what is already there.

    It deliberately STOPS at the storyboard gate rather than approving on your
    behalf. Approval is where render budget is allocated, and a one-button build
    that silently cleared it would defeat the only spend control in the pipeline.
    """
    try:
        sb = get_current_project()
        if not sb.shots:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "This project has no beats. Draft a storyboard first."})

        def fn():
            ep = config.episode_paths(sb.title)
            note = lambda m: log_job("rough_cut", m)

            def have(d, ids):
                try:
                    return {p.stem for p in d.iterdir() if p.is_file()} & ids
                except OSError:
                    return set()

            ids = {s.scene_id for s in sb.shots}
            n = len(sb.shots)

            # 1 — stills
            missing = [s for s in sb.shots if not s.draft_image]
            if missing:
                note(f"[1/5] Generating stills for {len(missing)} beat(s) ...")
                assets.generate_drafts(sb, n=_takes(sb), backend=sb.render.backend,
                                       save_fn=save_current_project, log=note)
                for s in sb.shots:
                    if s.draft_variations and s.chosen_variation is None:
                        s.chosen_variation = 0
                        s.draft_image = s.draft_variations[0]
                save_current_project(sb)
            else:
                note(f"[1/5] Stills: all {n} beats already illustrated.")

            # 2 — the gate. Not ours to clear.
            if not sb.storyboard_approved:
                note("")
                note("STOPPED at the storyboard gate.")
                note("Approve the storyboard in Step 1 to allocate render budget, "
                     "then press Build rough cut again — it resumes from here.")
                return

            # 3 — narration, then SFX
            if not sb.script_locked:
                sb.script_locked = True
                save_current_project(sb)
                note("Script locked (the storyboard is approved).")
            have_narr = have(ep["narration"], ids)
            if len(have_narr) < n:
                note(f"[2/5] Recording narration for {n - len(have_narr)} beat(s) ...")
                audio.synthesize_narration(sb)
            else:
                note(f"[2/5] Narration: all {n} beats already voiced.")

            # Always re-fit durations, even when nothing was recorded. Skipping
            # this on a resume left every beat at the 6.0s default while the run
            # reported success -- a 25-beat episode came out as 150s of uniform
            # slots with the narration overrunning every cut.
            before = {s.scene_id: float(s.camera.duration) for s in sb.shots if s.camera}
            report: dict = {}
            changed = audio.sync_durations(sb, report=report)
            save_current_project(sb)

            # A clip rendered at the old length is simply the wrong length, and
            # the render step below skips any beat that already has one. Drop the
            # stale local clips so the resume converges instead of keeping a cut
            # whose video and timing disagree. Paid Tier-C clips are never
            # deleted -- they cost money and are re-timed by padding instead.
            retimed = [s for s in sb.shots
                       if s.camera and abs(before.get(s.scene_id, 0.0)
                                           - float(s.camera.duration)) > 0.05
                       and s.motion_type != MotionType.AI_VIDEO
                       # A beat assembled from a locked director plan owns its
                       # clip regardless of tier. Deleting a compiled parallax
                       # coverage beat here would throw away a real compile and
                       # silently fall back to a single-shot render.
                       and not director.has_locked_coverage(s.scene_id)]
            if retimed:
                rdir = ep["render"]
                for s in retimed:
                    (rdir / f"{s.scene_id}.mp4").unlink(missing_ok=True)
                note(f"  {len(retimed)} beat(s) changed length — their clips will be re-rendered.")
            total = sum(float(s.camera.duration) for s in sb.shots if s.camera)
            note(f"  timings: {changed} beat(s) refitted to their voiceover; "
                 f"runtime {total:.1f}s (~{total/60:.1f} min).")
            for o in report.get("overrun", []):
                note(f"  !! {o['scene_id']}: locked at {o['duration']:.1f}s but its "
                     f"narration runs {o['vo']:.1f}s — the voice will overrun.")

            sfx_dir = ep["sfx"]
            sfx_dir.mkdir(parents=True, exist_ok=True)
            wanted = [s for s in sb.shots if (s.sfx or "").strip()]
            todo = [s for s in wanted if not (sfx_dir / f"{s.scene_id}.mp3").exists()]
            if todo:
                note(f"[3/5] Generating ambience for {len(todo)} beat(s) ...")
                for s in todo:
                    try:
                        audio.generate_sfx_fal(s.sfx, sfx_dir / f"{s.scene_id}.mp3",
                                               duration_seconds=s.camera.duration)
                    except Exception as exc:  # noqa: BLE001
                        note(f"  !! {s.scene_id} SFX failed: {exc}")
            else:
                note(f"[3/5] Ambience: {len(wanted)} beat(s) already generated.")

            # 4 — render
            note("[4/5] Rendering beats ...")
            generate_fal_and_render(sb, force_paid=force_paid, log=note)

            # 5 — assemble
            note("[5/5] Building the preview and the Resolve timeline ...")
            out, runtime = timeline.build_preview(sb)
            timeline.build(sb)
            note(f"Rough cut ready: {_safe_rel_path(out)} ({runtime:.1f}s, "
                 f"~{runtime/60:.1f} min). FCPXML exported.")

        start_job("rough_cut", fn)
        return {"ok": True, "stage": "rough_cut"}
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
                    n=_takes(sb),
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
            require_paid_gate(sb, "render")
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


SFX_UPLOAD_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def _layer_path(sb, scene_id: str, layer_id: str) -> Path:
    return config.episode_paths(sb.title)["sfx"] / f"{scene_id}__{layer_id}.mp3"


@app.get("/api/shot/{scene_id}/layers")
def list_layers(scene_id: str):
    """This beat's SFX layers, including the legacy single file as a layer."""
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        sfx_dir = config.episode_paths(sb.title)["sfx"]
        out = []
        for lay in audio.resolve_sfx_layers(shot, sfx_dir):
            d = asdict(lay)
            f = Path(lay.file) if lay.file else (sfx_dir / f"{scene_id}.mp3")
            d["url"] = config.rel_media_path(f) if f.is_file() else None
            out.append(d)
        return {"ok": True, "layers": out}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/layers")
async def add_or_update_layer(scene_id: str, request: Request):
    """Create or edit a layer. Send `id` to edit, omit it to create.

    Editing is sparse: only the keys present change, so nudging an offset cannot
    clobber a gain set moments earlier from another surface.
    """
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        data = await request.json()

        # Materialise the legacy single file before adding a second layer, or it
        # would silently disappear the moment `sfx_layers` becomes non-empty.
        if not shot.sfx_layers:
            shot.sfx_layers = list(
                audio.resolve_sfx_layers(shot, config.episode_paths(sb.title)["sfx"])
            )

        lid = (data.get("id") or "").strip()
        lay = next((l for l in shot.sfx_layers if l.id == lid), None) if lid else None
        if lay is None:
            import time
            lay = manifest.AudioLayer(id=data.get("id") or f"L{int(time.time()*1000)%10**8}")
            shot.sfx_layers.append(lay)

        for k, lo, hi in (("gain", 0.0, 4.0), ("offset", -120.0, 120.0),
                          ("fade_in", 0.0, 30.0), ("fade_out", 0.0, 30.0)):
            if k in data and data[k] is not None:
                setattr(lay, k, max(lo, min(hi, float(data[k]))))
        for k in ("prompt", "label", "source", "file"):
            if k in data and data[k] is not None:
                setattr(lay, k, str(data[k]))

        save_current_project(sb)
        return {"ok": True, "layer": asdict(lay), "count": len(shot.sfx_layers)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/layers/{layer_id}/delete")
def delete_layer(scene_id: str, layer_id: str):
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        if not shot.sfx_layers:
            shot.sfx_layers = list(
                audio.resolve_sfx_layers(shot, config.episode_paths(sb.title)["sfx"])
            )
        before = len(shot.sfx_layers)
        shot.sfx_layers = [l for l in shot.sfx_layers if l.id != layer_id]
        # The audio file is left on disk deliberately: removing a layer from the
        # mix should not destroy a clip that was paid for or uploaded.
        save_current_project(sb)
        return {"ok": True, "removed": before - len(shot.sfx_layers)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/layers/{layer_id}/generate")
def generate_layer(scene_id: str, layer_id: str):
    """Generate this layer's audio from its own prompt."""
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        lay = next((l for l in (shot.sfx_layers or []) if l.id == layer_id), None)
        if lay is None:
            raise HTTPException(status_code=404, detail="Layer not found")
        if not (lay.prompt or "").strip():
            raise HTTPException(status_code=400, detail="This layer has no prompt.")

        dest = _layer_path(sb, scene_id, layer_id)

        def fn():
            dest.parent.mkdir(parents=True, exist_ok=True)
            audio.generate_sfx_fal(lay.prompt, dest, duration_seconds=shot.camera.duration)
            lay.file = config.rel_media_path(dest) or str(dest)
            lay.source = "generated"
            save_current_project(sb)
            log_job("sfx", f"{scene_id}/{layer_id}: generated from '{lay.prompt[:50]}'")

        start_job("sfx", fn)
        return {"ok": True, "scene_id": scene_id, "layer_id": layer_id}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/shot/{scene_id}/layers/upload")
async def upload_layer_audio(scene_id: str, file: UploadFile = File(...)):
    """Bring your own sound. Transcoded to MP3 like anything generated."""
    try:
        sb = get_current_project()
        shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Scene not found")
        name = secure_filename(file.filename or "layer")
        if Path(name).suffix.lower() not in SFX_UPLOAD_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio type. Use one of: {', '.join(sorted(SFX_UPLOAD_SUFFIXES))}",
            )
        if not shot.sfx_layers:
            shot.sfx_layers = list(
                audio.resolve_sfx_layers(shot, config.episode_paths(sb.title)["sfx"])
            )

        import time
        lid = f"U{int(time.time()*1000)%10**8}"
        raw = _layer_path(sb, scene_id, lid).with_suffix(Path(name).suffix.lower())
        raw.parent.mkdir(parents=True, exist_ok=True)
        with open(raw, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        try:
            final = audio.transcode_to_mp3(raw, bitrate="128k")
        except Exception as exc:  # noqa: BLE001 — keep the upload even if ffmpeg balks
            print(f"layer upload: transcode failed ({exc}); keeping {raw.suffix}")
            final = raw

        lay = manifest.AudioLayer(
            id=lid, source="uploaded", label=Path(name).stem[:40],
            file=config.rel_media_path(final) or str(final),
        )
        shot.sfx_layers.append(lay)
        save_current_project(sb)
        return {"ok": True, "layer": asdict(lay), "count": len(shot.sfx_layers)}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/assemble/status")
def get_assemble_status():
    # Filtered to the bound project by get_jobs_status(); this endpoint has no
    # way to ask for another project's jobs, which is deliberate.
    return {"ok": True, "jobs": get_jobs_status(),
            "project_id": projects.require().project_id}


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
        cache_path = config.project_dir() / "_peaks_cache.json"
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
    path = (config.project_dir() / f"{slug}_bundle.zip").resolve()
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
    path = (config.project_dir() / f"{slug}.{kind}").resolve()
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
