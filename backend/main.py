"""FastAPI Application entry point for the YouTube Video Automation Studio.

Routes requests to the Firestore database models, ElevenLabs audio generator,
fal.ai media API, and DaVinci Resolve OTIO exporter.
"""

from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

import fal_client
import anthropic
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
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
from . import config, manifest, script, assets, audio, motion, timeline, sizzle
from .manifest import Storyboard, Shot, MotionType, Camera, RenderConfig, db
from .pipeline_worker import start_job, get_jobs_status

app = FastAPI(title="YouTube Automation Studio API")

# Enable CORS for Next.js frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """Retrieve the currently active storyboard manifest."""
    active_path = get_active_manifest_path()
    f_id = get_project_id_from_path(active_path)
    try:
        sb = manifest.load_project(f_id)
    except Exception as fe:
        print(f"Warning: Firestore load_project failed: {fe}")
        sb = None
    if not sb:
        # Fallback to local file load
        sb = manifest.load(Path(active_path))
        sb.id = f_id
        # Ingest to Firestore
        try:
            manifest.save_project(sb)
        except Exception as fe:
            print(f"Warning: Firestore save_project failed: {fe}")
    # Refresh config manifest path dynamically in case it's in a subdirectory
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
    """Discover all storyboard_manifest.json projects under WORKSPACE_ROOT and GCS."""
    active = Path(get_active_manifest_path()).resolve()
    
    roots = [WORKSPACE_ROOT.resolve()]
    gcs_root = Path("/gcs").resolve()
    if gcs_root.exists() and gcs_root not in roots:
        roots.append(gcs_root)
        
    projects: list[dict] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune assets, references, source, and hidden dirs to avoid heavy scans
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and d not in ("assets", "references", "source") and not d.startswith(".")]
            if "storyboard_manifest.json" in filenames:
                mf = Path(dirpath) / "storyboard_manifest.json"
                
                folder_name = mf.parent.name
                if mf.parent == root:
                    folder_name = "Cloud GCS (root)" if root == gcs_root else "Local App (root)"
                
                name = ""
                channel = "bestiary"
                if "calluses" in str(mf.resolve()).lower():
                    channel = "calluses"
                try:
                    if mf.exists():
                        manifest_data = json.loads(mf.read_text(encoding="utf-8"))
                        name = (manifest_data.get("title") or "").strip()
                        if "channel" in manifest_data:
                            channel = manifest_data.get("channel") or channel
                except Exception:
                    pass
                if not name:
                    name = folder_name

                try:
                    rel_display = mf.relative_to(WORKSPACE_ROOT.resolve())
                except ValueError:
                    try:
                        rel_display = mf.relative_to(gcs_root)
                    except ValueError:
                        rel_display = mf.name

                projects.append({
                    "name": name,
                    "rel": str(mf.resolve()).replace("\\", "/"),
                    "rel_display": str(rel_display).replace("\\", "/"),
                    "active": mf.resolve() == active,
                    "channel": channel,
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
    if not path_str:
        return None
    p_raw = str(path_str).replace("\\", "/").strip()
    p_clean = p_raw.lstrip("/")
    active_dir = Path(get_active_manifest_path()).parent

    parts = p_clean.split("/")
    filename = parts[-1]
    sid = scene_id or (parts[-2] if len(parts) >= 2 else None)

    candidates = []
    # 1. Project-specific scene directory (highest priority)
    if sid:
        candidates.append(active_dir / "assets" / sid / filename)
        candidates.append(active_dir / sid / filename)
        
    # 2. General active dir / clean path
    candidates.append(active_dir / p_clean)

    allowed_roots = [WORKSPACE_ROOT.resolve(), Path("/gcs").resolve()]
    for cand in candidates:
        try:
            res = cand.resolve()
            if res.exists() and res.is_file():
                if any(res == root or root in res.parents for root in allowed_roots):
                    return res
        except Exception:
            pass
    return None


def _safe_rel_path(dest: Path) -> str:
    try:
        return str(dest.relative_to(config.ROOT)).replace("\\", "/")
    except ValueError:
        return str(dest).replace("\\", "/").lstrip("/")


VALID_CLAUDE_MODELS = {
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet-20250219",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
}

DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-20241022"


def normalize_claude_model(model_name: str | None) -> str:
    """Normalize any shorthand or custom model string strictly to valid Anthropic API model endpoints."""
    if not model_name:
        return DEFAULT_CLAUDE_MODEL

    m = str(model_name).strip().lower()

    if m in VALID_CLAUDE_MODELS:
        return m

    if "3-7" in m or "3.7" in m:
        return "claude-3-7-sonnet-20250219"
    if "opus" in m:
        return "claude-3-opus-20240229"
    if "haiku" in m:
        return "claude-3-5-haiku-20241022"
    if "latest" in m:
        return "claude-3-5-sonnet-latest"

    return DEFAULT_CLAUDE_MODEL


def create_claude_message(client, model, max_tokens, system, messages):
    norm_model = normalize_claude_model(model)
    models_to_try = [norm_model]
    fallbacks = [
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-latest",
        "claude-3-5-sonnet-20240620",
        "claude-3-7-sonnet-20250219",
        "claude-3-haiku-20240307",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229"
    ]
    for fb in fallbacks:
        if fb not in models_to_try:
            models_to_try.append(fb)

    first_exc = None
    last_exc = None
    for m in models_to_try:
        try:
            print(f"Trying Claude model: {m}...")
            return client.messages.create(
                model=m,
                max_tokens=max_tokens,
                system=system,
                messages=messages
            )
        except Exception as exc:
            print(f"Claude model {m} failed: {exc}")
            if first_exc is None:
                first_exc = exc
            last_exc = exc
            continue

    if first_exc:
        err_msg = str(first_exc)
        if "not_found" in err_msg.lower() or "404" in err_msg:
            raise RuntimeError(
                f"Anthropic API Key Error (404 Not Found): None of the standard Claude models were accessible with your current API key. "
                f"Please verify your CLAUDE_API_KEY secret in Cloud Run or ensure your Anthropic account has access to the Claude Messages API."
            ) from first_exc
        raise first_exc
    if last_exc:
        raise last_exc



def resolve_video_model_endpoint(key: str | None) -> str:
    if not key:
        return "bytedance/seedance-2.0/image-to-video"

    k = str(key).strip().lower()
    MAP = {
        "seedance_2_0": "bytedance/seedance-2.0/image-to-video",
        "seedance": "bytedance/seedance-2.0/image-to-video",
        "seedance-2.0": "bytedance/seedance-2.0/image-to-video",
        "seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",
        "bytedance/seedance-2.0": "bytedance/seedance-2.0/image-to-video",
        "bytedance/seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",
        "fal-ai/bytedance/seedance-2.0": "bytedance/seedance-2.0/image-to-video",
        "fal-ai/bytedance/seedance-2.0/image-to-video": "bytedance/seedance-2.0/image-to-video",

        "veo_3_1": "fal-ai/veo3.1/image-to-video",
        "veo": "fal-ai/veo3.1/image-to-video",
        "veo_3": "fal-ai/veo3.1/image-to-video",
        "veo-video": "fal-ai/veo3.1/image-to-video",

        "kling_2_5_turbo_pro": "fal-ai/kling-video/v3/image-to-video",
        "kling": "fal-ai/kling-video/v3/image-to-video",
        "kling_v3": "fal-ai/kling-video/v3/image-to-video",
        "kling-video": "fal-ai/kling-video/v3/image-to-video",

        "wan_2_7": "fal-ai/wan/v2.7/image-to-video",
        "wan": "fal-ai/wan/v2.7/image-to-video",
        "wan-video": "fal-ai/wan/v2.7/image-to-video",

        "hunyuan_video": "fal-ai/hunyuan-video/image-to-video",
        "hunyuan": "fal-ai/hunyuan-video/image-to-video",

        "luma_dream_machine": "fal-ai/luma-dream-machine/ray-2/image-to-video",
        "luma": "fal-ai/luma-dream-machine/ray-2/image-to-video",
    }

    if k in MAP:
        return MAP[k]

    if "seedance" in k:
        return "bytedance/seedance-2.0/image-to-video"
    if "veo" in k:
        return "fal-ai/veo3.1/image-to-video"
    if "kling" in k:
        return "fal-ai/kling-video/v3/image-to-video"
    if "wan" in k:
        return "fal-ai/wan/v2.7/image-to-video"
    if "hunyuan" in k:
        return "fal-ai/hunyuan-video/image-to-video"
    if "luma" in k:
        return "fal-ai/luma-dream-machine/ray-2/image-to-video"

    if k.startswith("fal-ai/"):
        return k
    return f"fal-ai/{k.lstrip('/')}"


def set_active_video_clip(sb: Storyboard, shot: Shot, video_rel_path: str, out_dir: Path):
    shot.video_clip = video_rel_path
    src_path = WORKSPACE_ROOT / video_rel_path
    dest_path = out_dir / f"{shot.scene_id}.mp4"
    if src_path.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        try:
            frame_out_path = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
            assets.extract_final_frame(dest_path, frame_out_path)
        except Exception as e:
            print(f"Error extracting final frame for {shot.scene_id}: {e}")


def generate_fal_and_render(sb: Storyboard) -> None:
    config.require_for("assets")
    
    out_dir = config.episode_paths(sb.title)["render"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    prev_extracted_frame: Path | None = None
    prev_video_dest_path: Path | None = None
    chaining_mode = getattr(sb.render, "video_chaining", "native_extend")

    for shot in sb.shots:
        if getattr(shot, "hero_clip", False):
            print(f"{shot.scene_id}: Already has imported hero clip - keeping it, not re-rendering.")
            prev_extracted_frame = None
            prev_video_dest_path = None
            continue
            
        # Ensure still image draft is generated
        if not shot.draft_image:
            backend = getattr(shot, "image_model", None) or getattr(sb.render, "backend", None) or "nano2"
            print(f"Generating drafts for {shot.scene_id} using {backend}...")
            assets.generate_for_shot(shot, n=3, backend=backend, render=sb.render)
            shot.chosen_variation = 0
            shot.draft_image = shot.draft_variations[0]
            save_current_project(sb)
            
        is_ai = (shot.motion_type == MotionType.AI_VIDEO)
        
        if is_ai:
            video_key = getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
            model_endpoint = resolve_video_model_endpoint(video_key)
            print(f"Generating paid video for {shot.scene_id} using {model_endpoint} (chaining: {chaining_mode})...")
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
                print(f"Native Video Extend: extending from previous segment {prev_video_dest_path.name}...")
                public_video_url = fal_client.upload_file(str(prev_video_dest_path))
                arguments["video_url"] = public_video_url
            else:
                # OpenCV final frame or initial still
                if chaining_mode != "independent" and prev_extracted_frame and prev_extracted_frame.exists():
                    local_image_path = prev_extracted_frame
                    print(f"Continuous flow: chaining from final frame -> {local_image_path.name}")
                else:
                    local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                    if not local_image_path or not local_image_path.exists():
                        print(f"Still image draft not found for {shot.scene_id}, generating still drafts...")
                        try:
                            assets.generate_for_shot(shot, n=3, backend=sb.render.backend, render=sb.render)
                            shot.chosen_variation = 0
                            shot.draft_image = shot.draft_variations[0]
                            save_current_project(sb)
                            local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                        except Exception as exc:
                            print(f"  !! Failed to generate still draft for {shot.scene_id}: {exc}")
                            continue

                if not local_image_path or not local_image_path.exists():
                    print(f"  !! Still image draft file missing on disk for {shot.scene_id}: {shot.draft_image}")
                    continue
                    
                print(f"Uploading starting image {local_image_path.name}...")
                public_image_url = fal_client.upload_file(str(local_image_path))
                arguments["image_url"] = public_image_url
            
            print(f"Triggering fal.ai API with prompt: {motion_prompt[:80]}...")
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

            print(f"Downloading generated video from {video_url} to {local_video_path}...")
            assets._download(video_url, local_video_path)

            video_rel_path = f"assets/{shot.scene_id}/{local_video_name}"
            if not hasattr(shot, "video_variations") or shot.video_variations is None:
                shot.video_variations = []
            shot.video_variations.append(video_rel_path)

            set_active_video_clip(sb, shot, video_rel_path, out_dir)
            save_current_project(sb)

            dest_video_path = out_dir / f"{shot.scene_id}.mp4"
            prev_video_dest_path = dest_video_path
            print(f"Successfully generated video for {shot.scene_id}")

            try:
                frame_out_path = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
                prev_extracted_frame = assets.extract_final_frame(dest_video_path, frame_out_path)
            except Exception as exc:
                print(f"Warning: Failed to extract final frame for continuous chaining on {shot.scene_id}: {exc}")
                prev_extracted_frame = None
        else:
            print(f"Rendering local video for {shot.scene_id} ({shot.motion_type.value})...")
            motion.render_shot(shot, fps=motion.DEFAULT_FPS, height=motion.DEFAULT_HEIGHT, out_dir=out_dir, placeholder=False)
            prev_extracted_frame = None
            prev_video_dest_path = None
            
    print("Generation and rendering complete!")


def migrate_assets_to_project_folders(gcs_root: Path):
    projects = [
        ("bestiary/manananggal", gcs_root / "bestiary" / "manananggal"),
        ("bestiary/leshy", gcs_root / "bestiary" / "leshy")
    ]
    global_assets_dir = gcs_root / "assets"
    if not global_assets_dir.exists():
        return
        
    for name, p_dir in projects:
        manifest_file = p_dir / "storyboard_manifest.json"
        if not manifest_file.exists():
            continue
            
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            shots = data.get("shots", [])
            for shot in shots:
                scene_id = shot.get("scene_id")
                if not scene_id:
                    continue
                    
                paths = []
                if shot.get("draft_image"):
                    paths.append(shot["draft_image"])
                if shot.get("video_clip"):
                    paths.append(shot["video_clip"])
                paths.extend(shot.get("draft_variations") or [])
                paths.extend(shot.get("video_variations") or [])
                
                for p_str in paths:
                    p_clean = str(p_str).replace("\\", "/").lstrip("/")
                    if p_clean.startswith("assets/"):
                        sub_path = p_clean[7:]
                    else:
                        sub_path = p_clean
                        
                    src_file = global_assets_dir / sub_path
                    if src_file.exists() and src_file.is_file():
                        dest_file = p_dir / "assets" / sub_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_file.exists() or dest_file.stat().st_size != src_file.stat().st_size:
                            shutil.copy2(src_file, dest_file)
                            print(f"Migration: Copied {src_file.name} to {dest_file}")
        except Exception as e:
            print(f"Migration warning: Failed to migrate assets for {name}: {e}")


def sanitize_manifest_image_paths(manifest_file: Path, project_dir: Path):
    if not manifest_file.exists():
        return
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        shots = data.get("shots", [])
        modified = False
        for s in shots:
            scene_id = s.get("scene_id")
            if not scene_id:
                continue
            
            # Verify draft_image
            draft_img = s.get("draft_image")
            if draft_img:
                clean_path = str(draft_img).replace("\\", "/").lstrip("/")
                sub_p = clean_path[7:] if clean_path.startswith("assets/") else clean_path
                
                proj_asset = project_dir / "assets" / sub_p
                global_asset = Path("/gcs/assets") / sub_p
                
                # If image is a legacy generic var_X.png or missing on disk/GCS, clear it!
                if "var_" in clean_path or not (proj_asset.exists() or global_asset.exists()):
                    s["draft_image"] = None
                    s["approved"] = False
                    modified = True
                    
            # Verify draft_variations
            vars_list = s.get("draft_variations") or []
            new_vars = []
            for v in vars_list:
                clean_v = str(v).replace("\\", "/").lstrip("/")
                sub_v = clean_v[7:] if clean_v.startswith("assets/") else clean_v
                p_v = project_dir / "assets" / sub_v
                g_v = Path("/gcs/assets") / sub_v
                if not "var_" in clean_v and (p_v.exists() or g_v.exists()):
                    new_vars.append(v)
            if len(new_vars) != len(vars_list):
                s["draft_variations"] = new_vars
                modified = True
                
        if modified:
            manifest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"Sanitized stale manifest image paths in {manifest_file}")
    except Exception as e:
        print(f"Warning: Failed to sanitize manifest {manifest_file}: {e}")


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

    # 2. Cleanup old duplicate files to keep workspace completely clean
    files_to_remove = [
        gcs_root / "storyboard_manifest.json",
        gcs_root / "bestiary" / "storyboard_manifest.json",
        gcs_root / "calluses" / "storyboard_manifest.json",
        WORKSPACE_ROOT / "storyboard_manifest.json",
        WORKSPACE_ROOT / "bestiary" / "storyboard_manifest.json",
        WORKSPACE_ROOT / "calluses" / "storyboard_manifest.json",
    ]
    for f in files_to_remove:
        try:
            if f.exists() and f.is_file():
                f.unlink()
                print(f"Cleanup: Removed obsolete duplicate manifest file {f}")
        except Exception as e:
            print(f"Cleanup warning: could not remove {f}: {e}")
            
    # Clean up empty calluses dir on local/GCS
    try:
        old_calluses_dir = gcs_root / "calluses"
        if old_calluses_dir.exists() and old_calluses_dir.is_dir():
            shutil.rmtree(old_calluses_dir)
            print(f"Cleanup: Removed old calluses folder {old_calluses_dir}")
    except Exception as e:
        pass

    # 3. Migrate assets to project subfolders
    try:
        migrate_assets_to_project_folders(gcs_root)
    except Exception as e:
        print(f"Startup Warning: migrate_assets_to_project_folders failed: {e}")

    # 4. Sanitize stale manifest paths
    try:
        sanitize_manifest_image_paths(manananggal_manifest, manananggal_dir)
        sanitize_manifest_image_paths(leshy_manifest, leshy_dir)
    except Exception as e:
        print(f"Startup Warning: sanitize_manifest_image_paths failed: {e}")


@app.on_event("startup")
async def startup_event():
    # 1. Ensure GCS default files
    try:
        ensure_gcs_projects()
    except Exception as e:
        print(f"Startup Warning: ensure_gcs_projects failed: {e}")

    # 2. Bootstrap Firestore from local scanned JSON manifests
    try:
        scanned = _scan_projects()
        for p in scanned:
            p_path = p["rel"]
            f_id = get_project_id_from_path(p_path)
            doc = db.collection("projects").document(f_id).get()
            if not doc.exists:
                print(f"Firestore Bootstrap: Loading {p['name']} from disk into Firestore ({f_id})...")
                sb = manifest.load(Path(p_path))
                sb.id = f_id
                manifest.save_project(sb)
    except Exception as e:
        print(f"Startup Warning: Firestore bootstrap failed: {e}")


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
            f_id = get_project_id_from_path(p["rel"])
            if f_id in db_map:
                p["name"] = db_map[f_id].get("title") or p["name"]
                p["channel"] = db_map[f_id].get("channel") or p["channel"]
                p["beats_count"] = db_map[f_id].get("beats_count", 0)
                p["script_locked"] = db_map[f_id].get("script_locked", False)
                p["storyboard_approved"] = db_map[f_id].get("storyboard_approved", False)
            else:
                p["beats_count"] = 0
                p["script_locked"] = False
                p["storyboard_approved"] = False
            res.append(p)
            
        if channel:
            res = [p for p in res if p["channel"] == channel]
        return {"ok": True, "projects": res}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/project/active")
def get_active_project():
    try:
        sb = get_current_project()
        reg = _ref_registry()
        
        # Prepare metadata structure
        shots_payload = []
        for s in sb.shots:
            s_dict = asdict(s)
            s_dict["motion_type"] = s.motion_type.value
            s_dict["references_resolved"] = [
                {"name": n, "file": _ref_file(n, reg)} for n in s.references
            ]
            s_dict["motion_prompt_suggestion"] = _suggest_motion_prompt(s)
            # Clip resolution path
            shot_paths = config.episode_paths(sb.title)
            dest_clip = shot_paths["render"] / f"{s.scene_id}.mp4"
            s_dict["active_clip_url"] = f"/render/{s.scene_id}.mp4" if dest_clip.exists() else None
            shots_payload.append(s_dict)
            
        # Preview track resolution
        ep = config.episode_paths(sb.title)
        preview_file = ep["render"] / "_preview.mp4"
        preview_url = f"/media/{_safe_rel_path(preview_file)}" if preview_file.exists() else None
        
        fcpxml_file = config.ROOT / f"{ep['slug']}.fcpxml"
        fcpxml_ready = fcpxml_file.exists()
        
        # Count paid video shots
        paid_count = len(sb.paid_shots())
        
        # Options map to pass to frontend
        image_backends = {
            "nano2": "Nano Banana 2",
            "flux-cfg": "Flux CFG",
        }
        
        video_backends = {
            "seedance_2_0": "Seedance 2.0 (image-to-video)",
            "veo_3_1": "Google Veo 3.1 (image-to-video)",
            "kling_2_5_turbo_pro": "Kling 2.5 Turbo Pro (image-to-video)",
            "wan_2_7": "Wan 2.7 (image-to-video)",
            "luma_dream_machine": "Luma Dream Machine Ray-2 (image-to-video)",
        }
        
        return {
            "ok": True,
            "project": {
                "id": sb.id,
                "title": sb.title,
                "channel": sb.channel,
                "cultural_origin": sb.cultural_origin,
                "script_locked": sb.script_locked,
                "storyboard_approved": sb.storyboard_approved,
                "render": asdict(sb.render),
                "shots": shots_payload,
            },
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
        
        p = Path(rel_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Project file not found on disk")
        
        set_active_manifest_path(rel_path)
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
        
        gcs_root = Path("/gcs/assets").resolve()
        if gcs_root.exists():
            scan_root = gcs_root / channel
            scan_root.mkdir(parents=True, exist_ok=True)
        else:
            scan_root = WORKSPACE_ROOT
            
        if not name:
            n = 1
            while (scan_root / f"project_{n}").exists():
                n += 1
            name = f"project_{n}"
            
        name = secure_filename(name)
        if not name:
            name = "untitled_project"
            
        proj_dir = scan_root / name
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
            
        save_current_project(sb)
        return {"ok": True, "paid_count": len(sb.paid_shots())}
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
        backend = data.get("backend") or getattr(sb.render, "backend", "nano2")
        
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
        
        client = anthropic.Anthropic()
        resp = create_claude_message(
            client=client,
            model=script.DEFAULT_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=messages
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        
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
        abs_path = WORKSPACE_ROOT / rel_path
        if abs_path.exists() and abs_path.is_file():
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
        abs_path = WORKSPACE_ROOT / rel_path
        if abs_path.exists() and abs_path.is_file():
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
            
        new_sb = script.generate_script(topic, num_beats=beats, channel=channel)
        new_sb.id = sb.id
        new_sb.title = sb.title
        
        save_current_project(new_sb)
        return {"ok": True, "shots": len(new_sb.shots)}
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
        
        new_sb = script.generate_script_from_messages(messages, num_beats=beats, channel=channel)
        new_sb.id = sb.id
        new_sb.title = sb.title
        
        save_current_project(new_sb)
        return {"ok": True, "shots": len(new_sb.shots)}
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
        client = anthropic.Anthropic()
        
        resp = create_claude_message(
            client=client,
            model=script.DEFAULT_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
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
def run_assemble_endpoint(stage: str):
    try:
        sb = get_current_project()
        
        if stage == "narration":
            if not sb.script_locked:
                if sb.storyboard_approved:
                    sb.script_locked = True
                    save_current_project(sb)
                else:
                    return JSONResponse(status_code=400, content={"ok": False, "error": "Lock the script first."})
            
            def fn():
                audio.synthesize_narration(sb)
                changed = audio.sync_durations(sb)
                save_current_project(sb)
                print(f"Voiceover generated; synced {changed} shot duration(s) to audio.")
                
        elif stage == "render":
            if not sb.storyboard_approved:
                return JSONResponse(status_code=400, content={"ok": False, "error": "Approve the storyboard first."})
            def fn():
                generate_fal_and_render(sb)
                # Auto-generate ambient SFX for all beats in the batch render pass
                sfx_dir = config.ASSETS / "sfx"
                sfx_dir.mkdir(parents=True, exist_ok=True)
                for shot in sb.shots:
                    if shot.sfx:
                        dest = sfx_dir / f"{shot.scene_id}.mp3"
                        if not dest.exists():
                            audio.generate_sfx_fal(shot.sfx, dest, duration_seconds=shot.camera.duration)
            
        elif stage == "preview":
            fn = lambda: timeline.build_preview(sb)
            
        elif stage == "timeline":
            fn = lambda: timeline.build(sb)
            
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
        
        res = audio.generate_voice_design_elevenlabs(gender, age, accent, description, sample_text)
        return {"ok": True, "voice": res}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/voice/settings")
async def voice_settings_endpoint(request: Request):
    try:
        data = await request.json()
        if "voice_id" in data:
            config.ELEVENLABS_VOICE_ID = str(data["voice_id"]).strip()
        if "stability" in data:
            config.ELEVENLABS_STABILITY = float(data["stability"])
        if "style_exaggeration" in data:
            config.ELEVENLABS_STYLE_EXAGGERATION = float(data["style_exaggeration"])
        return {
            "ok": True,
            "voice_id": config.ELEVENLABS_VOICE_ID,
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
            
        sfx_dir = config.ASSETS / "sfx"
        sfx_dir.mkdir(parents=True, exist_ok=True)
        dest = sfx_dir / f"{scene_id}.mp3"
        
        out_path = audio.generate_sfx_fal(shot.sfx, dest, duration_seconds=shot.camera.duration)
        rel = _safe_rel_path(out_path)
        return {"ok": True, "path": rel}
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/assemble/status")
def get_assemble_status():
    return {"ok": True, "jobs": get_jobs_status()}


# --- MEDIA SERVING ENDPOINTS ---

@app.get("/media/{filepath:path}")
def serve_media_files(filepath: str):
    clean = filepath.replace("\\", "/").lstrip("/")
    active_dir = Path(get_active_manifest_path()).parent

    # Enforce strict project-isolated lookups
    candidates = [
        active_dir / clean,
        active_dir / "assets" / clean,
        config.REFERENCES_DIR / clean,
    ]

    parts = clean.split("/")
    filename = parts[-1]
    if len(parts) >= 2:
        scene_id = parts[-2]
        candidates.append(active_dir / "assets" / scene_id / filename)
        candidates.append(active_dir / scene_id / filename)

    allowed_roots = [WORKSPACE_ROOT.resolve(), Path("/gcs").resolve()]
    for cand in candidates:
        try:
            res = cand.resolve()
            if res.exists() and res.is_file():
                if any(res == root or root in res.parents for root in allowed_roots):
                    return FileResponse(res, headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"})
        except Exception:
            pass

    raise HTTPException(status_code=404, detail=f"Media not found: {filepath}")


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
