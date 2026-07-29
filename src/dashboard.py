"""Dashboard: the local Web UI Studio Workspace (and the Storyboard/Budget gate).

Beyond the original Gate-1 review (pick a frame per beat, set motion tier, Approve),
this is now the working studio surface:

* **Workspace** — a sidebar folder-tree of every directory under the project root
  that holds a ``storyboard_manifest.json``; pick one to make it the active project.
* **Generation knobs** — edit this project's ``Storyboard.render`` (guidance_scale,
  real_cfg_scale, num_inference_steps, negative_prompt override); saved straight into
  the manifest and consumed by ``assets.py``.
* **Develop (Claude)** — ``/chat/develop`` proxies chat to Claude with Vesper's
  ethnographic-documentary system prompt; a topic can be turned into a structured
  storyboard via ``script.generate_script`` and locked via ``script.lock_script``.
* **Shot cards** — edit narration / scene / style_medium, pick a draft, choose the
  MotionType, flag a manual **VEO/Flow hero**, and drag-drop reference images.

Everything reads and writes through the native ``manifest`` dataclasses + ``load`` /
``save`` — no parallel state.

Run:
    python -m src.dashboard
    -> open http://127.0.0.1:5000
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template_string, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

from . import config
from .manifest import MotionType, load, save

app = Flask(__name__)

DEFAULT_VIDEO_MODEL = "fal-ai/kling-video/v3/image-to-video"
TIER_LABEL = {"static": "A · still + FX ($0)", "parallax": "B · parallax ($0)",
              "ai_video": "C · AI video (paid)"}

# Image-model backends offered in the UI (label + rough per-image cost).
BACKENDS = {
    "nano2": "Nano Banana 2 (~$0.15)",
    "flux-cfg": "flux-general (~$0.04)",
    "flux_2_max": "FLUX.2 [max] (~$0.07)",
    "flux_2_pro": "FLUX.2 [pro] (~$0.08)",
    "flux_1_1_pro_ultra": "FLUX 1.1 [pro] Ultra (~$0.12)",
    "flux_1_dev_turbo": "FLUX.1 [dev] Turbo (~$0.02)",
    "ideogram_4": "Ideogram 4.0 (~$0.08)",
    "ideogram_4_instant": "Ideogram 4.0 Instant (~$0.02)",
    "wan_2_7_image": "Wan 2.7 Image (~$0.06)"
}
ALLOWED_BACKENDS = {"nano2", "flux-cfg", "nano", "flux", "flux-lora", "flux_2_max", "flux_2_pro", "flux_1_1_pro_ultra", "flux_1_dev_turbo", "ideogram_4", "ideogram_4_instant", "wan_2_7_image"}

# Video-model backends offered in the UI.
VIDEO_BACKENDS = {
    "seedance_2_0": "Seedance 2.0 (Native Extend / Image-to-Video)",
    "veo_3_1": "Veo 3.1 (High Detail Image-to-Video)",
    "kling_2_5_turbo_pro": "Kling 2.5 Turbo Pro (Fast Motion)",
    "wan_2_7": "Wan 2.7 (Cinematic B-Roll)",
    "hunyuan_video": "Hunyuan Video (Extend / Image-to-Video)",
    "luma_dream_machine": "Luma Ray 2 (Native Extend / Image-to-Video)",
}

# Root under which we look for sibling projects, and dirs we never descend into.
WORKSPACE_ROOT = config.ROOT
IGNORE_DIRS = {".venv", ".git", "__pycache__", "assets", "audio", "audio_pool",
               "lora_training", "render", "models", "sizzle", "intro", "references",
               "node_modules", "scripts", "src", "tmp", "temp", "output", "cache"}

# Active project setup
ACTIVE_PROJECT_FILE = Path("/gcs/.active_project") if Path("/gcs").exists() else (WORKSPACE_ROOT / ".active_project")

def _get_active_manifest_path() -> Path:
    if ACTIVE_PROJECT_FILE.exists():
        try:
            path_str = ACTIVE_PROJECT_FILE.read_text(encoding="utf-8").strip()
            if path_str:
                p = Path(path_str)
                if p.exists():
                    return p
        except Exception:
            pass
    return config.MANIFEST_PATH

_state = {"manifest": _get_active_manifest_path()}
config.set_active_manifest(_state["manifest"])

def _set_active_manifest_path(path: Path) -> None:
    _state["manifest"] = path
    try:
        ACTIVE_PROJECT_FILE.write_text(str(path.resolve()), encoding="utf-8")
    except Exception:
        pass

# Spend guard: cap paid image regenerations per server process. Raise via env.
REGEN_LIMIT = int(os.environ.get("STUDIO_REGEN_LIMIT", "20"))
_regen_count = {"n": 0}

# Background jobs for the long back-half stages (narration / render / timeline).
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _start_job(stage: str, fn) -> bool:
    """Run ``fn`` in a daemon thread, capturing stdout + status. False if busy."""
    with _jobs_lock:
        if _jobs.get(stage, {}).get("status") == "running":
            return False
        _jobs[stage] = {"status": "running", "log": "", "started": time.time()}

    def worker():
        buf = io.StringIO()
        status = "done"
        try:
            with contextlib.redirect_stdout(buf):
                fn()
        except Exception:
            buf.write("\n" + traceback.format_exc())
            status = "error"
        with _jobs_lock:
            _jobs[stage].update(status=status, log=buf.getvalue()[-4000:], ended=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# state helpers — everything routes through manifest.load / manifest.save
# --------------------------------------------------------------------------- #
def _load():
    manifest_path = Path(_state["manifest"]).resolve()
    sb = load(manifest_path)
    if not sb.shots and manifest_path.name == "storyboard_manifest.json" and manifest_path.parent == Path("/gcs").resolve():
        projects = _scan_projects()
        for p in projects:
            p_path = Path(p["rel"]).resolve()
            if p_path != manifest_path:
                try:
                    candidate = load(p_path)
                    if candidate.shots:
                        _state["manifest"] = p_path
                        _set_active_manifest_path(p_path)
                        config.set_active_manifest(p_path)
                        return candidate
                except Exception:
                    pass
    config.set_active_manifest(_state["manifest"])
    return load(_state["manifest"])


def _save(sb) -> None:
    save(sb, _state["manifest"])


def _find(sb, scene_id: str):
    return next((s for s in sb.shots if s.scene_id == scene_id), None)


def _paid_count(sb) -> int:
    return len(sb.paid_shots())


def _scan_projects() -> list[dict]:
    """Every dir under WORKSPACE_ROOT and/or /gcs holding a storyboard_manifest.json."""
    active = Path(_state["manifest"]).resolve()
    
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
    """First local filename backing a reference name, if any (for UI thumbnails)."""
    entry = reg.get(name) or {}
    files = entry.get("files") or ([entry["file"]] if entry.get("file") else [])
    return files[0] if files else None


def _suggest_motion_prompt(shot) -> str:
    """A copy-ready image-to-video prompt for a video/hero shot (start frame = the still)."""
    base = ". ".join(p.strip() for p in (shot.style_medium, shot.prompt) if p and p.strip())
    dur = shot.camera.duration if shot.camera else 6.0
    return (
        f"{base}. Animate this still as the start frame with subtle, restrained in-world "
        f"motion — slow drift, mist/smoke, faint flicker, a gradual reveal; hold the "
        f"composition, no camera cuts. Target length ~{dur:.0f}s."
    ).strip(". ").strip()


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #
PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ sb.title or "Untitled" }} — Studio</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #09090b; }
    ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #3f3f46; }
    #toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#09090b;
      border:1px solid #f59e0b; color:#f59e0b; padding:10px 18px; border-radius:8px;
      opacity:0; transition:.2s; pointer-events:none; z-index:100; font-weight: 500; font-size: 13px; } #toast.show { opacity:1; }
  </style>
</head>
<body class="bg-zinc-950 text-zinc-200 font-sans min-h-screen flex flex-col">

<header class="sticky top-0 z-50 bg-zinc-900/90 backdrop-blur-md border-b border-zinc-800 px-6 py-4 flex flex-col sm:flex-row items-center justify-between gap-4">
  <div class="flex flex-wrap items-center gap-3 w-full sm:w-auto">
    <!-- Editable Title -->
    <input type="text" id="project_title" value="{{ sb.title or 'Untitled' }}" onchange="saveProjectMeta()"
           class="bg-zinc-950 text-amber-400 font-bold px-3 py-1.5 rounded-lg text-lg border border-zinc-800 focus:outline-none focus:border-amber-400 w-full sm:w-64 md:w-80 transition"
           placeholder="Project Title">
           
    <!-- Channel Select -->
    <select id="project_channel" onchange="saveProjectMeta()"
            class="bg-zinc-950 text-zinc-300 text-sm px-3 py-2 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
      <option value="bestiary" {{ 'selected' if sb.channel == 'bestiary' else '' }}>The Illuminated Bestiary</option>
      <option value="calluses" {{ 'selected' if sb.channel == 'calluses' else '' }}>By the Calluses</option>
    </select>
    
    <!-- Meta Info -->
    <span class="text-zinc-500 text-xs bg-zinc-950/50 px-3 py-1.5 rounded-md border border-zinc-900/80 font-mono">
      <span class="text-amber-500 font-semibold">{{ sb.shots|length }}</span> beats · 
      <span class="text-amber-500 font-semibold" id="paidCount">{{ paid }}</span> Tier-C · 
      <span class="text-zinc-400">{{ sb.cultural_origin or "no culture set" }}</span> · 
      <span class="text-zinc-400">script: <strong class="text-zinc-300">{{ "locked" if sb.script_locked else "draft" }}</strong></span>
    </span>
  </div>
  
  <div class="flex items-center gap-3 justify-end w-full sm:w-auto">
    <span class="text-xs px-2.5 py-1 rounded-full border {{ 'border-emerald-800 bg-emerald-950/30 text-emerald-400' if sb.storyboard_approved else 'border-zinc-800 bg-zinc-900/50 text-zinc-400' }}">
      {{ "Approved ✓" if sb.storyboard_approved else "Draft" }}
    </span>
    <button class="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-semibold px-4 py-2 rounded-lg shadow-lg hover:shadow-amber-500/10 transition active:scale-95 text-sm" onclick="approve()">Approve storyboard →</button>
  </div>
</header>

<div class="flex flex-1">
  <!-- Projects Sidebar -->
  <aside class="w-64 bg-zinc-900/50 border-r border-zinc-800 p-4 shrink-0 hidden lg:flex flex-col gap-4 min-h-screen">
    <div>
      <button onclick="newProject()" class="w-full bg-amber-500 hover:bg-amber-600 text-zinc-950 font-bold py-2 px-4 rounded-lg shadow-lg transition active:scale-95 text-sm flex items-center justify-center gap-2">
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
        </svg>
        <span>New Storyboard</span>
      </button>
    </div>
    
    <div class="flex-1 flex flex-col gap-4 overflow-y-auto">
      <div>
        <h2 class="text-[10px] uppercase tracking-wider text-zinc-500 font-bold mb-2 px-2 border-b border-zinc-800 pb-1">The Illuminated Bestiary</h2>
        <div class="flex flex-col gap-1.5">
          {% for p in projects if p.channel == 'bestiary' %}
            <div class="proj group p-3 rounded-lg border border-transparent hover:bg-zinc-800/50 cursor-pointer transition {{ 'bg-zinc-800 border-amber-500/30 text-amber-400 font-medium' if p.active else 'text-zinc-450 hover:text-zinc-200' }}" onclick="selectProject('{{ p.rel }}')">
              <div class="text-sm truncate font-medium">{{ p.name }}</div>
              <div class="text-[10px] text-zinc-550 group-hover:text-zinc-400 truncate font-mono mt-0.5">{{ p.rel_display }}</div>
            </div>
          {% else %}
            <div class="text-zinc-650 text-xs italic p-2 pl-4">No bestiary storyboards</div>
          {% endfor %}
        </div>
      </div>
      
      <div>
        <h2 class="text-[10px] uppercase tracking-wider text-zinc-500 font-bold mb-2 px-2 border-b border-zinc-800 pb-1">By the Calluses</h2>
        <div class="flex flex-col gap-1.5">
          {% for p in projects if p.channel == 'calluses' %}
            <div class="proj group p-3 rounded-lg border border-transparent hover:bg-zinc-800/50 cursor-pointer transition {{ 'bg-zinc-800 border-amber-500/30 text-amber-400 font-medium' if p.active else 'text-zinc-450 hover:text-zinc-200' }}" onclick="selectProject('{{ p.rel }}')">
              <div class="text-sm truncate font-medium">{{ p.name }}</div>
              <div class="text-[10px] text-zinc-550 group-hover:text-zinc-400 truncate font-mono mt-0.5">{{ p.rel_display }}</div>
            </div>
          {% else %}
            <div class="text-zinc-650 text-xs italic p-2 pl-4">No calluses storyboards</div>
          {% endfor %}
        </div>
      </div>
    </div>
  </aside>

  <!-- Main Workspace -->
  <main class="flex-1 max-w-6xl mx-auto p-6 flex flex-col gap-6">

    <!-- Knobs Panel -->
    <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-6">
      <h3 class="text-amber-400 font-semibold text-base mb-4">Generation Knobs (this project)</h3>
      
      <div class="flex flex-wrap items-center gap-6 mb-4 border-b border-zinc-800/50 pb-4">
        <div class="flex flex-col gap-2">
          <label class="text-zinc-400 text-sm font-medium">Default Image Models (Multi-select)</label>
          <div class="flex flex-wrap gap-4 bg-zinc-950 p-3 rounded-lg border border-zinc-800">
            {% for v,label in backends.items() %}
            <label class="flex items-center gap-2 text-zinc-300 text-xs cursor-pointer hover:text-zinc-100 select-none">
              <input type="checkbox" name="k_backend_checkbox" value="{{ v }}"
                     {{ 'checked' if (v in render.backend.split(',')) else '' }}
                     class="rounded bg-zinc-900 border-zinc-800 text-amber-500 focus:ring-amber-500/20">
              <span>{{ label }}</span>
            </label>
            {% endfor %}
          </div>
        </div>

        <div class="flex items-center gap-2">
          <label class="text-amber-400 text-sm font-medium">Default Video Model (fal.ai)</label>
          <select id="k_video_model" class="bg-zinc-950 text-amber-300 text-sm px-3 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
            {% for vk, vlabel in video_backends.items() %}
            <option value="{{ vk }}" {{ 'selected' if render.video_model==vk else '' }}>{{ vlabel }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="flex items-center gap-2">
          <label class="text-zinc-400 text-sm font-medium">Sequence Flow</label>
          <select id="k_video_chaining" class="bg-zinc-950 text-zinc-300 text-sm px-3 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
            <option value="native_extend" {{ 'selected' if (not render.video_chaining or render.video_chaining=='native_extend') else '' }}>Native Video Extend (Seedance/Luma)</option>
            <option value="opencv_chain" {{ 'selected' if render.video_chaining=='opencv_chain' else '' }}>OpenCV Chained Final Frame</option>
            <option value="independent" {{ 'selected' if render.video_chaining=='independent' else '' }}>Independent Still Drafts</option>
          </select>
        </div>

        <div class="flex items-center gap-2">
          <label class="text-zinc-400 text-sm font-medium font-mono text-amber-400">🔊 Video Audio</label>
          <select id="k_video_audio" class="bg-zinc-950 text-zinc-300 text-sm px-3 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
            <option value="true" {{ 'selected' if (render.video_audio==True or render.video_audio is not defined) else '' }}>Enabled (Seedance native sound)</option>
            <option value="false" {{ 'selected' if render.video_audio==False else '' }}>Disabled (Silent clip)</option>
          </select>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
        <div>
          <label class="block text-zinc-400 text-xs font-medium mb-1">guidance_scale</label>
          <input type="number" step="0.1" id="k_guidance" value="{{ render.guidance_scale }}" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition">
        </div>
        <div>
          <label class="block text-zinc-400 text-xs font-medium mb-1">nag_scale (neg strength)</label>
          <input type="number" step="0.1" id="k_nag" value="{{ render.nag_scale }}" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition">
        </div>
        <div>
          <label class="block text-zinc-400 text-xs font-medium mb-1">num_inference_steps</label>
          <input type="number" step="1" id="k_steps" value="{{ render.num_inference_steps }}" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition">
        </div>
      </div>

      <div class="mb-4">
        <label class="block text-zinc-400 text-xs font-medium mb-1">negative_prompt override (blank = built-in default)</label>
        <textarea id="k_negative" placeholder="{{ default_negative }}" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-2 h-20 focus:outline-none focus:border-amber-400 transition resize-y">{{ render.negative_prompt }}</textarea>
      </div>

      <div class="flex flex-wrap items-center gap-4 border-t border-zinc-800/50 pt-4 mb-4">
        <label class="text-zinc-400 text-sm">Global frame reference</label>
        {% if render.reference_image %}
          <div class="flex items-center gap-2 bg-zinc-950/50 p-1 pr-3 border border-zinc-850 rounded-lg">
            <img src="{{ media_url(render.reference_image) }}" class="h-10 w-16 object-cover border border-zinc-800 rounded">
            <button onclick="clearFrame()" class="text-zinc-400 hover:text-zinc-200 text-xs font-semibold px-2 py-1 bg-zinc-850 rounded transition">✕ remove</button>
          </div>
        {% else %}
          <span class="text-xs text-zinc-500 italic">none — shots may drift to different borders</span>
        {% endif %}
        <div class="border border-dashed border-zinc-800 hover:border-amber-400/50 rounded-lg px-4 py-2 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer transition" id="framedrop"
             ondragover="event.preventDefault();this.classList.add('border-amber-500')"
             ondragleave="this.classList.remove('border-amber-500')"
             ondrop="dropFrame(event)"
             onclick="document.getElementById('framefile').click()">⬆ set frame (border/page-edge)</div>
        <input type="file" id="framefile" accept="image/*" style="display:none" onchange="uploadFrame(this.files[0])">
        <span class="text-xs text-zinc-500">Nano Banana 2 only</span>
      </div>

      <div>
        <button onclick="saveRender()" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 px-4 py-2 rounded-lg border border-zinc-700 transition">Save knobs</button>
      </div>
    </div>

    <!-- Vesper Panel -->
    <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-6">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-amber-400 font-semibold text-base">Develop with Vesper (Claude) &amp; script gate</h3>
        <span class="text-xs text-zinc-500">Active Channel: <strong class="text-zinc-300 capitalize" id="current_channel_label">{{ sb.channel }}</strong></span>
      </div>

      <!-- Chat History Pane -->
      <div class="chatlog h-80 overflow-y-auto bg-zinc-950 border border-zinc-850 rounded-lg p-4 mb-4 flex flex-col gap-3 scroll-smooth" id="chatlog">
        <div class="text-zinc-500 text-sm italic text-center py-12">Start typing below to develop the concept with Vesper...</div>
      </div>

      <!-- Chat Input and Controls -->
      <div class="flex gap-2 mb-6">
        <input type="text" id="chatinput" placeholder="Ask Vesper to develop the entity / angle…" 
               class="flex-1 bg-zinc-950 text-zinc-100 placeholder-zinc-500 border border-zinc-800 rounded-lg px-4 py-2 focus:outline-none focus:border-amber-400 transition"
               onkeydown="if(event.key==='Enter')chatSend()">
        <button onclick="chatSend()" class="bg-zinc-850 hover:bg-zinc-800 text-zinc-200 px-4 py-2 rounded-lg border border-zinc-750 transition text-sm">Send</button>
        <button class="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-semibold px-4 py-2 rounded-lg transition text-sm shadow-md" onclick="scriptFromChat()">Use chat → script</button>
      </div>

      <!-- Storyboard Generation Area -->
      <div class="flex flex-col md:flex-row gap-3 border-t border-zinc-800/80 pt-6">
        <div class="flex-1 flex gap-2">
          <input type="text" id="gen_topic" placeholder="Entity / topic to draft a full storyboard…" 
                 class="flex-1 bg-zinc-950 text-zinc-100 placeholder-zinc-500 border border-zinc-800 rounded-lg px-4 py-2 focus:outline-none focus:border-amber-400 transition">
          <input type="number" id="gen_beats" placeholder="beats" 
                 class="w-20 bg-zinc-950 text-zinc-100 placeholder-zinc-500 border border-zinc-800 rounded-lg px-3 py-2 text-center focus:outline-none focus:border-amber-400 transition" min="1">
        </div>
        <div class="flex gap-2">
          <button onclick="genStoryboard()" class="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-semibold px-5 py-2 rounded-lg transition shadow-md text-sm">Draft storyboard</button>
          <button onclick="lockScript()" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-750 px-5 py-2 rounded-lg transition text-sm">🔒 Lock script</button>
        </div>
      </div>

      <!-- Storyboard Visual Grid Overview -->
      {% if sb.shots %}
      <div class="mt-6 border-t border-zinc-800/80 pt-6">
        <h4 class="text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-3">Storyboard Beats Overview</h4>
        <div class="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-3">
          {% for s in sb.shots %}
          <a href="#beat-{{ s.scene_id }}" class="group block bg-zinc-950 border border-zinc-850 hover:border-amber-400/50 rounded-lg p-2 transition">
            <div class="aspect-video bg-zinc-900 rounded overflow-hidden mb-1.5 relative border border-zinc-800">
              {% if s.draft_image %}
              <img src="{{ media_url(s.draft_image) }}" class="w-full h-full object-cover group-hover:scale-105 transition duration-300" loading="lazy">
              {% else %}
              <div class="w-full h-full flex items-center justify-center text-zinc-650 text-[10px]">No draft</div>
              {% endif %}
              <div class="absolute top-1 left-1 bg-zinc-950/80 backdrop-blur-xs text-[9px] text-amber-500 font-mono px-1 py-0.5 rounded border border-zinc-850">
                {{ s.scene_id }}
              </div>
            </div>
            <p class="text-[10px] text-zinc-400 line-clamp-2 leading-tight group-hover:text-zinc-200 transition">
              {{ s.narration or s.prompt }}
            </p>
          </a>
          {% endfor %}
        </div>
      </div>
      {% endif %}
    </div>

    <!-- Assemble Panel -->
    {% if sb.storyboard_approved %}
    <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-6">
      <h3 class="text-amber-400 font-semibold text-base mb-4">Assemble — storyboard approved ✓</h3>
      <div class="flex flex-col gap-4">
        
        <div class="flex items-center">
          <button id="btn-narration" onclick="assemble('narration',this)" 
                  class="inline-flex items-center gap-2 bg-zinc-800 hover:bg-zinc-700 disabled:bg-zinc-950 disabled:text-zinc-600 text-zinc-105 px-4 py-2 rounded-lg border border-zinc-700 disabled:border-zinc-850 font-medium text-sm transition active:scale-95">
            <svg class="animate-spin h-4 w-4 text-amber-500 hidden mr-1" id="spinner-narration" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span>1 · Generate narration</span>
          </button>
          <span id="st-narration" class="text-xs text-zinc-550 ml-4 font-mono"></span>
        </div>

        <div class="border border-zinc-800 rounded-lg p-4 bg-zinc-950/40 flex flex-col gap-4">
          <div class="text-xs font-semibold text-zinc-400 uppercase tracking-wider">2 · Render &amp; Ingest Media</div>
          
          <div class="flex flex-wrap items-center gap-4">
            <!-- Option A: Generate via fal.ai -->
            <button id="btn-render" onclick="assemble('render',this)" 
                    class="inline-flex items-center gap-2 bg-zinc-800 hover:bg-zinc-700 disabled:bg-zinc-950 disabled:text-zinc-600 text-zinc-105 px-4 py-2.5 rounded-lg border border-zinc-700 disabled:border-zinc-850 font-medium text-sm transition active:scale-95">
              <svg class="animate-spin h-4 w-4 text-amber-500 hidden mr-1" id="spinner-render" viewBox="0 0 24 24" fill="none">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              <span>Option A: Generate via fal.ai</span>
            </button>
            <span id="st-render" class="text-xs text-zinc-550 font-mono"></span>
          </div>
          
          <!-- Option B: Drag-and-drop Upload Reference zone -->
          <div class="border-t border-zinc-800/80 pt-4 flex flex-col md:flex-row items-center gap-4">
            <div class="flex items-center gap-2 w-full md:w-auto">
              <label class="text-xs text-zinc-400 whitespace-nowrap">Option B (Bypass): Target Beat</label>
              <select id="bypass_beat_select" class="bg-zinc-950 text-zinc-300 text-xs px-2.5 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
                {% for s in sb.shots %}
                <option value="{{ s.scene_id }}">{{ s.scene_id }} ({{ (s.narration or s.prompt)[:30] }}...)</option>
                {% endfor %}
              </select>
            </div>
            
            <div class="border border-dashed border-zinc-805 hover:border-amber-400/50 rounded-lg px-4 py-3 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer transition flex-1 text-center w-full" 
                 id="bypass_drop_zone"
                 ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                 ondragleave="this.classList.remove('border-amber-500')"
                 ondrop="dropBypass(event)"
                 onclick="document.getElementById('bypass_file_input').click()">
              Drag &amp; drop or click to upload image/video for selected beat (Bypasses generation)
            </div>
            <input type="file" id="bypass_file_input" accept="image/*,video/*" style="display:none" onchange="uploadBypass(this.files[0])">
          </div>
        </div>

        <div class="flex items-center">
          <button id="btn-preview" onclick="assemble('preview',this)" 
                  class="inline-flex items-center gap-2 bg-zinc-800 hover:bg-zinc-700 disabled:bg-zinc-950 disabled:text-zinc-600 text-zinc-105 px-4 py-2 rounded-lg border border-zinc-700 disabled:border-zinc-850 font-medium text-sm transition active:scale-95">
            <svg class="animate-spin h-4 w-4 text-amber-500 hidden mr-1" id="spinner-preview" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span>3 · Build preview (watch the cut)</span>
          </button>
          <span id="st-preview" class="text-xs text-zinc-550 ml-4 font-mono"></span>
        </div>

        <div class="flex items-center">
          <button id="btn-timeline" onclick="assemble('timeline',this)" 
                  class="inline-flex items-center gap-2 bg-zinc-800 hover:bg-zinc-700 disabled:bg-zinc-950 disabled:text-zinc-600 text-zinc-105 px-4 py-2 rounded-lg border border-zinc-700 disabled:border-zinc-850 font-medium text-sm transition active:scale-95">
            <svg class="animate-spin h-4 w-4 text-amber-500 hidden mr-1" id="spinner-timeline" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span>4 · Build DaVinci timeline (OTIO + FCPXML)</span>
          </button>
          <span id="st-timeline" class="text-xs text-zinc-550 ml-4 font-mono"></span>
        </div>
      </div>
      
      {% if paid %}<div class="text-xs text-zinc-500 mt-4">{{ paid }} Tier-C (ai_video) shot(s) render as placeholders until the paid fal video stage (Seedance/Kling) is wired.</div>{% endif %}
      {% if heroes %}<div class="text-xs text-zinc-500 mt-1">{{ heroes }} shot(s) flagged VEO/Flow hero — hand-animate those in Flow and import the clips.</div>{% endif %}
      
      {% if preview_url %}
      <div class="mt-4 pt-4 border-t border-zinc-800">
        <video controls playsinline class="w-full max-h-96 bg-black rounded-lg border border-zinc-800 shadow-md" src="{{ preview_url }}?v={{ range(100000)|random }}"></video>
        <div class="text-xs text-zinc-550 mt-2">Assembled preview — narration + music + all clips. This is the review proxy, not the master.</div>
      </div>
      {% endif %}
      
      {% if fcpxml_ready %}
      <div class="text-sm mt-3 text-amber-400 font-medium">▶ Next step: open <b>{{ ep_slug }}.fcpxml</b> in DaVinci Resolve (File → Import → Timeline) to finish the master cut.</div>
      {% endif %}
    </div>
    {% endif %}

    <!-- Beats List -->
    {% for s in sb.shots %}
      <div class="beat bg-zinc-900 border border-zinc-800 rounded-xl p-6 transition duration-200 {{ 'border-emerald-800/40 bg-emerald-950/5' if s.approved else '' }} {{ 'border-amber-500/40 bg-amber-950/5' if s.flow_hero else '' }} {{ 'border-indigo-800/40 bg-indigo-950/5' if s.motion_type.value=='ai_video' else '' }}" id="beat-{{ s.scene_id }}">
        <div class="beat-top flex flex-col md:flex-row gap-6 items-start">
          <!-- Left Scene Metadata Badge -->
          <div class="flex md:flex-col items-center justify-between md:justify-start gap-2 min-w-[70px] w-full md:w-auto">
            <span class="text-amber-500 font-mono text-sm font-bold bg-zinc-950 border border-zinc-800 px-2.5 py-1 rounded shadow-md">{{ s.scene_id }}</span>
            <span class="text-zinc-500 text-xs font-mono">⏱ {{ '%.1f'|format(s.camera.duration) }}s</span>
          </div>
          
          <!-- Center Text Fields -->
          <div class="flex-1 w-full flex flex-col gap-4">
            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">Narration · {{ '%.1f'|format(s.camera.duration) }}s slot</label>
              <textarea onchange="saveField('{{ s.scene_id }}','narration',this.value)" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition font-sans">{{ s.narration }}</textarea>
            </div>
            
            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">Scene (Visual)</label>
              <textarea onchange="saveField('{{ s.scene_id }}','prompt',this.value)" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition font-sans">{{ s.prompt }}</textarea>
            </div>
            
            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">Style Medium</label>
              <input type="text" value="{{ s.style_medium }}" onchange="saveField('{{ s.scene_id }}','style_medium',this.value)" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition">
            </div>
            
            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">🎬 Video-Gen Prompt (Veo/Flow/Seedance) · animate to ~{{ '%.0f'|format(s.camera.duration) }}s</label>
              <textarea id="mp-{{ s.scene_id }}" onchange="saveField('{{ s.scene_id }}','motion_prompt',this.value)" class="w-full bg-zinc-950 text-zinc-100 border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition">{{ s.motion_prompt or motion_suggest[s.scene_id] }}</textarea>
              
              <div class="flex flex-wrap items-center gap-3 mt-2">
                <button onclick="copyText('mp-{{ s.scene_id }}')" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs px-3 py-1.5 rounded-lg border border-zinc-700 transition">Copy prompt</button>
                <div class="border border-dashed border-zinc-800 hover:border-amber-400/50 rounded-lg px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer transition" id="clipdrop-{{ s.scene_id }}"
                     ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                     ondragleave="this.classList.remove('border-amber-500')"
                     ondrop="dropClip(event,'{{ s.scene_id }}')"
                     onclick="document.getElementById('clipfile-{{ s.scene_id }}').click()">⬆ import hero clip (Veo/Flow)</div>
                <input type="file" id="clipfile-{{ s.scene_id }}" accept="video/*" style="display:none"
                       onchange="uploadClip('{{ s.scene_id }}',this.files[0])">
                <span class="text-xs text-zinc-500 font-mono">{% if s.hero_clip %}✓ hero clip imported{% else %}target ~{{ '%.0f'|format(s.camera.duration) }}s{% endif %}</span>
              </div>
            </div>

            <!-- Per-Shot Prompt Refiner Chatbox -->
            <div class="mt-4 bg-zinc-950/60 border border-zinc-800/80 rounded-xl p-4 shadow-inner">
              <div class="flex items-center justify-between mb-2 pb-2 border-b border-zinc-800/50">
                <span class="text-xs font-semibold text-amber-400 font-mono flex items-center gap-1.5 select-none">
                  💬 Vesper Chat Refiner ({{ s.scene_id }})
                </span>
                <button id="apply-btn-{{ s.scene_id }}" onclick="applyRefinedPrompts('{{ s.scene_id }}')" class="hidden bg-emerald-800 hover:bg-emerald-700 text-emerald-100 text-[10px] px-2.5 py-1 rounded font-bold transition flex items-center gap-1 shadow">
                  ✨ Apply Refined Prompts
                </button>
              </div>

              <!-- Chat Messages Logs -->
              <div id="chatlog-{{ s.scene_id }}" class="max-h-36 overflow-y-auto flex flex-col gap-2 mb-3 text-[11px] bg-zinc-950 p-3 rounded-lg border border-zinc-900 font-mono">
                <div class="text-zinc-600 italic select-none">Describe issues with the current renders to converse with Vesper, then click apply to save the new prompts.</div>
              </div>

              <!-- Input Row -->
              <div class="flex gap-2">
                <input type="text" id="chatinput-{{ s.scene_id }}" placeholder="e.g. 'make the scene darker, add thick smoke, pan slower'"
                       onkeydown="if(event.key==='Enter') sendShotChat('{{ s.scene_id }}')"
                       class="flex-1 bg-zinc-950 text-zinc-100 text-xs border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition">
                <button id="send-btn-{{ s.scene_id }}" onclick="sendShotChat('{{ s.scene_id }}')" class="bg-amber-500 hover:bg-amber-600 text-zinc-950 text-xs font-semibold px-4 py-2 rounded-lg transition shadow-md select-none">
                  Send
                </button>
              </div>
            </div>
          </div>
          
          <!-- Right Controls Column -->
          <div class="w-full md:w-48 flex flex-col gap-4 border-t md:border-t-0 md:border-l border-zinc-805 pt-4 md:pt-0 md:pl-6">
            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">Motion Tier</label>
              <select onchange="saveField('{{ s.scene_id }}','motion_type',this.value)" class="w-full bg-zinc-950 text-zinc-300 text-sm px-2.5 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
                {% for v,label in tiers.items() %}
                <option value="{{ v }}" {{ 'selected' if s.motion_type.value==v else '' }}>{{ label }}</option>
                {% endfor %}
              </select>
            </div>

            <div>
              <label class="block text-amber-400 text-xs font-semibold uppercase tracking-wider mb-1.5">Video Model (fal)</label>
              <select onchange="saveField('{{ s.scene_id }}','video_model',this.value)" class="w-full bg-zinc-950 text-amber-300 text-xs px-2 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
                {% for vk, vlabel in video_backends.items() %}
                <option value="{{ vk }}" {{ 'selected' if (s.video_model==vk or (not s.video_model and render.video_model==vk)) else '' }}>{{ vlabel }}</option>
                {% endfor %}
              </select>
            </div>

            <div>
              <label class="block text-zinc-400 text-xs font-semibold uppercase tracking-wider mb-1.5">🔊 Video Audio</label>
              <select onchange="saveField('{{ s.scene_id }}','video_audio',this.value==='none' ? null : (this.value==='true'))" class="w-full bg-zinc-950 text-zinc-300 text-xs px-2 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer">
                <option value="none" {{ 'selected' if s.video_audio is none else '' }}>Use Global Knob</option>
                <option value="true" {{ 'selected' if s.video_audio==True else '' }}>Enabled (Sound)</option>
                <option value="false" {{ 'selected' if s.video_audio==False else '' }}>Disabled (Silent)</option>
              </select>
            </div>
            
            <label class="flex items-center gap-2 text-zinc-400 text-xs font-medium cursor-pointer hover:text-zinc-200 select-none">
              <input type="checkbox" {{ 'checked' if s.flow_hero else '' }}
                onchange="saveField('{{ s.scene_id }}','flow_hero',this.checked)" class="rounded bg-zinc-950 border-zinc-800 text-amber-500 focus:ring-amber-500/20"> 
              <span>VEO/Flow hero</span>
            </label>
          </div>
        </div>
        
        <!-- Draft Variations Grid -->
        <div class="mt-6 pt-6 border-t border-zinc-800/80">
          <div class="flex items-center justify-between mb-3">
            <h4 class="text-zinc-400 text-xs font-semibold uppercase tracking-wider">
              Draft Variations {% if s.draft_variations %}<span class="text-amber-400 font-mono">({{ s.draft_variations|length }})</span>{% endif %}
            </h4>
            {% if s.draft_variations %}
            <span class="text-[10px] text-zinc-500">Click any tile to set as active frame</span>
            {% endif %}
          </div>
          {% if s.draft_variations %}
          <div class="grid grid-cols-3 gap-4">
            {% for path in s.draft_variations %}
            <div class="group relative rounded-lg overflow-hidden cursor-pointer bg-zinc-950 aspect-video border-2 {{ 'border-amber-400 shadow-amber-500/5' if s.chosen_variation==loop.index0 else 'border-transparent hover:border-zinc-700' }}"
                 onclick="pick('{{ s.scene_id }}',{{ loop.index0 }},this)">
              <img src="{{ media_url(path) }}" loading="lazy" class="w-full h-full object-cover">
              <div class="absolute top-2 left-2 bg-zinc-950/80 backdrop-blur-xs text-zinc-300 text-[10px] font-mono px-1.5 py-0.5 rounded border border-zinc-800/80 select-none">
                #{{ loop.index }}
              </div>
              <div class="absolute top-2 right-2 bg-amber-400 text-zinc-950 rounded-full w-5 h-5 flex items-center justify-center text-[10px] font-bold shadow-lg transition {{ 'opacity-100' if s.chosen_variation==loop.index0 else 'opacity-0 group-hover:opacity-30' }}">
                ✓
              </div>
              <!-- Delete Image Button -->
              <button onclick="event.stopPropagation(); deleteImage('{{ s.scene_id }}', {{ loop.index0 }})"
                      class="absolute bottom-2 right-2 bg-red-600/85 hover:bg-red-600 text-white rounded p-1 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                      title="Delete this image">
                🗑️
              </button>
              <!-- Edit Image Button (Image-to-Image) -->
              <button onclick="event.stopPropagation(); editImage('{{ s.scene_id }}', {{ loop.index0 }})"
                      class="absolute bottom-2 left-2 bg-amber-500 hover:bg-amber-600 text-zinc-950 rounded p-1 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                      title="Edit this image (Image-to-Image)">
                ✏️
              </button>
            </div>
            {% endfor %}
          </div>
          {% else %}
          <div class="text-sm text-zinc-550 italic py-2">No drafts generated yet. Use the controls below to generate.</div>
          {% endif %}
        </div>

        <!-- Video Variations Grid -->
        <div class="mt-6 pt-6 border-t border-zinc-800/80">
          <div class="flex items-center justify-between mb-3">
            <h4 class="text-zinc-400 text-xs font-semibold uppercase tracking-wider">
              🎬 Video Renders {% if s.video_variations %}<span class="text-amber-400 font-mono">({{ s.video_variations|length }})</span>{% endif %}
            </h4>
            {% if s.video_variations %}
            <span class="text-[10px] text-zinc-500">Hover to play · Click to select active video</span>
            {% endif %}
          </div>
          {% if s.video_variations %}
          <div class="grid grid-cols-3 gap-4">
            {% for vpath in s.video_variations %}
            <div class="group relative rounded-lg overflow-hidden cursor-pointer bg-zinc-950 aspect-video border-2 {{ 'border-amber-400 shadow-amber-500/5' if s.video_clip==vpath else 'border-transparent hover:border-zinc-700' }}"
                 onclick="pickVideo('{{ s.scene_id }}',{{ loop.index0 }},this)">
              <video src="{{ media_url(vpath) }}" muted loop class="w-full h-full object-cover" onmouseenter="this.play()" onmouseleave="this.pause(); this.currentTime=0;"></video>
              <div class="absolute top-2 left-2 bg-zinc-950/80 backdrop-blur-xs text-zinc-300 text-[10px] font-mono px-1.5 py-0.5 rounded border border-zinc-800/80 select-none">
                #{{ loop.index }}
              </div>
              <div class="absolute top-2 right-2 bg-amber-400 text-zinc-950 rounded-full w-5 h-5 flex items-center justify-center text-[10px] font-bold shadow-lg transition {{ 'opacity-100' if s.video_clip==vpath else 'opacity-0 group-hover:opacity-30' }}">
                ✓
              </div>
              <!-- Delete Video Button -->
              <button onclick="event.stopPropagation(); deleteVideo('{{ s.scene_id }}', {{ loop.index0 }})"
                      class="absolute bottom-2 right-2 bg-red-600/85 hover:bg-red-600 text-white rounded p-1 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                      title="Delete this video">
                🗑️
              </button>
            </div>
            {% endfor %}
          </div>
          {% else %}
          <div class="text-[11px] text-zinc-600 italic py-1">No video renders generated yet.</div>
          {% endif %}
        </div>
        
        <!-- Video Player for Rendered Clip -->
        {% if shot_clips[s.scene_id] %}
        <div class="mt-4">
          <video controls playsinline preload="none"
                 {% if s.draft_image %}poster="{{ media_url(s.draft_image) }}"{% endif %}
                 class="w-full max-h-60 bg-black rounded-lg border border-zinc-800 shadow-inner"
                 src="{{ shot_clips[s.scene_id] }}?v={{ range(100000)|random }}"></video>
          <div class="text-xs text-zinc-550 mt-2">▶ {% if s.hero_clip %}imported hero clip (Veo/Flow){% else %}rendered {{ s.motion_type.value }} clip{% endif %}</div>
        </div>
        {% endif %}
        
        <!-- References -->
        <div class="mt-4 flex flex-wrap items-center gap-3 border-t border-zinc-800/40 pt-4">
          {% for r in shot_refs[s.scene_id] %}
            <div class="relative w-16 h-10 border border-zinc-800 rounded overflow-hidden group bg-zinc-950" title="{{ r.name }}">
              {% if r.file %}
                <img src="{{ media_url(r.file) }}" class="w-full h-full object-cover">
              {% else %}
                <div class="w-full h-full flex items-center justify-center text-[10px] text-zinc-500 p-1 text-center leading-tight">{{ r.name }}</div>
              {% endif %}
              <span class="absolute top-0 right-0 w-4 h-4 leading-none text-center text-[10px] cursor-pointer bg-zinc-950/80 text-amber-500 hover:bg-rose-600 hover:text-white rounded-bl border-b border-l border-zinc-800 flex items-center justify-center opacity-0 group-hover:opacity-100 transition" title="remove reference" onclick="removeRef('{{ s.scene_id }}','{{ r.name }}')">✕</span>
            </div>
          {% endfor %}
          <div class="border border-dashed border-zinc-800 hover:border-amber-400/50 rounded-lg px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer transition" id="drop-{{ s.scene_id }}"
               ondragover="event.preventDefault();this.classList.add('border-amber-500')"
               ondragleave="this.classList.remove('border-amber-500')"
               ondrop="dropRef(event,'{{ s.scene_id }}')"
               onclick="document.getElementById('file-{{ s.scene_id }}').click()">+ drop / click to add reference</div>
          <input type="file" id="file-{{ s.scene_id }}" accept="image/*" style="display:none"
                 onchange="uploadRef('{{ s.scene_id }}',this.files[0])">
        </div>
        
        <!-- Action Row -->
        <div class="mt-4 flex flex-wrap items-center gap-3 border-t border-zinc-800/40 pt-4">

          <button onclick="regen('{{ s.scene_id }}',this)" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 px-3.5 py-1.5 rounded-lg border border-zinc-700 text-xs font-semibold transition active:scale-95">↻ Regenerate Still</button>
          <button onclick="generateShotVideo('{{ s.scene_id }}',this)" class="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-semibold px-3.5 py-1.5 rounded-lg transition text-xs shadow-md active:scale-95 flex items-center gap-1.5">
            <span>🎬 Generate Video</span>
          </button>
          <div class="border border-dashed border-zinc-800 hover:border-amber-400/50 rounded-lg px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer transition" id="imgdrop-{{ s.scene_id }}"
               ondragover="event.preventDefault();this.classList.add('border-amber-500')"
               ondragleave="this.classList.remove('border-amber-500')"
               ondrop="dropImage(event,'{{ s.scene_id }}')"
               onclick="document.getElementById('imgfile-{{ s.scene_id }}').click()">⬆ Upload finished image (use as draft)</div>
          <input type="file" id="imgfile-{{ s.scene_id }}" accept="image/*" style="display:none"
                 onchange="uploadImage('{{ s.scene_id }}',this.files[0])">
        </div>
      </div>
    {% endfor %}
  </main>
</div>

<div id="toast"></div>

<script>
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),1800); }
async function post(url,body){
  try {
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    const ct=r.headers.get('content-type')||'';
    let data;
    if(ct.includes('application/json')){
      data=await r.json();
    } else {
      const text=await r.text();
      data={error: text.length > 200 ? text.substring(0,200) + '...' : text};
    }
    return {ok:r.ok, data:data};
  } catch(e) {
    return {ok:false, data:{error:e.message}};
  }
}

function copyText(id){ const el=document.getElementById(id); if(!el) return;
  navigator.clipboard.writeText(el.value).then(()=>toast('prompt copied')).catch(()=>{ el.select(); document.execCommand('copy'); toast('prompt copied'); }); }

async function saveField(sid,field,val){ const b={}; b[field]=val; const {data}=await post('/api/shot/'+sid,b);
  if(field==='motion_type'){ document.getElementById('paidCount').textContent=data.paid_count;
    document.getElementById('beat-'+sid).classList.toggle('tierC',val==='ai_video'); }
  if(field==='flow_hero'){ document.getElementById('beat-'+sid).classList.toggle('hero',val); }
  toast(sid+' saved'); }

async function saveProjectMeta() {
  const title = document.getElementById('project_title').value;
  const channel = document.getElementById('project_channel').value;
  const {ok, data} = await post('/api/project/meta', {title, channel});
  if (ok) {
    toast('Project settings saved');
    document.getElementById('current_channel_label').textContent = data.channel;
  } else {
    toast('Failed to save project settings');
  }
}

async function saveRender(){ const checkedBackends = Array.from(document.querySelectorAll('input[name="k_backend_checkbox"]:checked')).map(cb => cb.value);
  const backendVal = checkedBackends.join(',') || 'nano2';
  const body={ backend:backendVal,
  video_model:document.getElementById('k_video_model').value,
  video_chaining:document.getElementById('k_video_chaining').value,
  video_audio:document.getElementById('k_video_audio').value === 'true',
  guidance_scale:parseFloat(document.getElementById('k_guidance').value),
  nag_scale:parseFloat(document.getElementById('k_nag').value),
  num_inference_steps:parseInt(document.getElementById('k_steps').value),
  negative_prompt:document.getElementById('k_negative').value };
  const {ok}=await post('/api/render',body); toast(ok?'knobs saved':'save failed'); }

async function pick(sid,idx,el){ el.parentNode.querySelectorAll('.group').forEach(v=>v.classList.remove('border-amber-400'));
  el.classList.add('border-amber-400'); await post('/api/shot/'+sid,{chosen_variation:idx}); toast(sid+' → variation '+(idx+1)); }

async function pickVideo(sid,idx,el){ el.parentNode.querySelectorAll('.group').forEach(v=>v.classList.remove('border-amber-400'));
  el.classList.add('border-amber-400'); await post('/api/shot/'+sid,{chosen_video_variation:idx}); toast(sid+' → video variation '+(idx+1)); setTimeout(()=>location.reload(),400); }

const shotChats = {};
async function sendShotChat(sid) {
  const inp = document.getElementById('chatinput-' + sid);
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';

  if (!shotChats[sid]) {
    shotChats[sid] = [];
  }
  shotChats[sid].push({ role: 'user', content: text });
  
  const logDiv = document.getElementById('chatlog-' + sid);
  const uMsg = document.createElement('div');
  uMsg.className = 'text-zinc-300 mb-1';
  uMsg.innerHTML = '<span class="text-amber-500 font-bold">You:</span> ' + escapeHtml(text);
  logDiv.appendChild(uMsg);
  logDiv.scrollTop = logDiv.scrollHeight;

  const sendBtn = document.getElementById('send-btn-' + sid);
  sendBtn.disabled = true;
  sendBtn.textContent = '…';
  inp.disabled = true;

  toast(sid + ': Vesper is thinking…');

  const { ok, data } = await post('/api/shot/' + sid + '/chat', { messages: shotChats[sid] });
  
  sendBtn.disabled = false;
  sendBtn.textContent = 'Send';
  inp.disabled = false;
  inp.focus();

  if (ok) {
    shotChats[sid].push({ role: 'assistant', content: data.reply });
    
    const aMsg = document.createElement('div');
    aMsg.className = 'text-amber-300/90 mb-2 leading-relaxed bg-amber-500/5 p-2 rounded border border-amber-500/10';
    aMsg.innerHTML = '<span class="text-amber-400 font-bold">Vesper:</span> ' + escapeHtml(data.reply);
    logDiv.appendChild(aMsg);
    logDiv.scrollTop = logDiv.scrollHeight;

    if (data.refined_prompt || data.refined_motion_prompt) {
      const applyBtn = document.getElementById('apply-btn-' + sid);
      applyBtn.classList.remove('hidden');
      applyBtn.dataset.refinedPrompt = data.refined_prompt || '';
      applyBtn.dataset.refinedMotionPrompt = data.refined_motion_prompt || '';
      toast(sid + ': Vesper proposed prompt refinements! Click Apply.');
    }
  } else {
    const errMsg = document.createElement('div');
    errMsg.className = 'text-rose-400 mb-1';
    errMsg.textContent = '[Error] ' + (data.error || 'Failed to communicate');
    logDiv.appendChild(errMsg);
    logDiv.scrollTop = logDiv.scrollHeight;
  }
}

async function applyRefinedPrompts(sid) {
  const applyBtn = document.getElementById('apply-btn-' + sid);
  const refinedPrompt = applyBtn.dataset.refinedPrompt;
  const refinedMotionPrompt = applyBtn.dataset.refinedMotionPrompt;

  toast(sid + ': applying refined prompts…');
  applyBtn.disabled = true;

  const { ok, data } = await post('/api/shot/' + sid + '/apply_chat_prompts', {
    refined_prompt: refinedPrompt || null,
    refined_motion_prompt: refinedMotionPrompt || null
  });

  if (ok) {
    toast(sid + ': prompts updated successfully!');
    setTimeout(() => location.reload(), 600);
  } else {
    alert('Failed to apply prompts.');
    applyBtn.disabled = false;
  }
}

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

async function deleteImage(sid, idx) {
  if (!confirm('Are you sure you want to delete this image variation?')) return;
  toast('Deleting image variation…');
  const { ok, data } = await post('/api/shot/' + sid + '/delete_image/' + idx);
  if (ok) {
    toast('Image variation deleted.');
    setTimeout(() => location.reload(), 400);
  } else {
    alert('Failed to delete image variation: ' + (data.error || 'unknown error'));
  }
}

async function editImage(sid, idx) {
  const p = prompt("Describe the changes you want to make to this image variation (Image-to-Image):", "");
  if (!p || !p.trim()) return;
  toast('Sending image edit request…');
  const { ok, data } = await post('/api/shot/' + sid + '/edit_image/' + idx, { prompt: p.trim() });
  if (ok) {
    toast('Edit variations generated!');
    setTimeout(() => location.reload(), 400);
  } else {
    alert('Failed to edit image: ' + (data.error || 'unknown error'));
  }
}

async function deleteVideo(sid, idx) {
  if (!confirm('Are you sure you want to delete this video variation?')) return;
  toast('Deleting video variation…');
  const { ok, data } = await post('/api/shot/' + sid + '/delete_video/' + idx);
  if (ok) {
    toast('Video variation deleted.');
    setTimeout(() => location.reload(), 400);
  } else {
    alert('Failed to delete video variation: ' + (data.error || 'unknown error'));
  }
}

async function regen(sid,btn){
  const checkedBackends = Array.from(document.querySelectorAll('input[name="k_backend_checkbox"]:checked')).map(cb => cb.value);
  const be = checkedBackends.join(',') || 'nano2';
  if(!confirm('⚠️ PAID: Regenerate calls via fal and counts against this session’s limit. Continue?')) return;
  btn.disabled=true; btn.textContent='↻ generating…';
  const {ok,data}=await post('/api/regenerate/'+sid,{backend:be}); btn.disabled=false; btn.textContent='↻ Regenerate Still';
  if(ok){ toast(sid+' regenerated ('+data.regen_used+'/'+data.regen_limit+', '+data.backend+')'); setTimeout(()=>location.reload(),500);}
  else { toast((data&&data.error)?data.error:'regen failed'); } }

async function generateShotVideo(sid,btn){
  const vmSelect = document.getElementById('vm-' + sid);
  const vm = vmSelect ? vmSelect.value : '';
  if(!confirm('🎬 PAID: Generate video clip for ' + sid + ' via fal.ai?')) return;
  btn.disabled = true;
  btn.textContent = '🎬 generating video…';
  toast(sid + ': sending video request to fal.ai…');
  const {ok, data} = await post('/api/shot/' + sid + '/generate_video', {video_model: vm});
  btn.disabled = false;
  btn.textContent = '🎬 Generate Video';
  if(ok){
    toast(sid + ' video generated successfully!');
    setTimeout(() => location.reload(), 600);
  } else {
    alert('Video generation failed:\\n' + (data.error || 'unknown error'));
  }
}

async function approve(){ const {ok,data}=await post('/api/approve');
  if(ok){ toast(data.gate_cleared?'Approved — paid stage unlocked':'Approved'); setTimeout(()=>location.reload(),700); }
  else { alert('Cannot approve yet:\\n'+(data.error||'')+'\\n'+(data.scenes||[]).join(', ')); } }

async function selectProject(rel){ const {ok}=await post('/api/project/select',{rel:rel});
  if(ok){ location.reload(); } else { toast('could not open project'); } }

async function newProject() {
  const name = prompt("Enter a name for the new storyboard / project:");
  if (name === null) return;
  const ch = document.getElementById('project_channel') ? document.getElementById('project_channel').value : 'bestiary';
  const {ok, data} = await post('/api/project/new', {name: name, channel: ch});
  if (ok) {
    toast('New project created');
    setTimeout(() => location.reload(), 500);
  } else {
    alert('Failed to create project: ' + (data.error || 'unknown error'));
  }
}

function dropBypass(ev) {
  ev.preventDefault();
  document.getElementById('bypass_drop_zone').classList.remove('border-amber-500');
  const f = ev.dragTransfer.files[0];
  if (f) uploadBypass(f);
}

async function uploadBypass(file) {
  if (!file) return;
  const sid = document.getElementById('bypass_beat_select').value;
  if (!sid) {
    toast('Select a beat first');
    return;
  }
  toast(`Uploading to ${sid}...`);
  
  const fd = new FormData();
  fd.append('file', file);
  
  let endpoint = '';
  if (file.type.startsWith('image/')) {
    endpoint = `/api/shot/${sid}/image`;
  } else if (file.type.startsWith('video/')) {
    endpoint = `/api/shot/${sid}/clip`;
  } else {
    toast('Unsupported file type (use image or video)');
    return;
  }
  
  try {
    const r = await fetch(endpoint, {method: 'POST', body: fd});
    const d = await r.json();
    if (d.ok) {
      toast(`Successfully uploaded to ${sid}!`);
      setTimeout(() => location.reload(), 600);
    } else {
      alert('Upload failed: ' + (d.error || 'unknown error'));
    }
  } catch (e) {
    alert('Upload error: ' + e);
  }
}

function addFile(sid,file){ const fd=new FormData(); fd.append('file',file);
  return fetch('/api/shot/'+sid+'/reference',{method:'POST',body:fd}).then(r=>r.json()); }
async function uploadRef(sid,file){ if(!file) return; const d=await addFile(sid,file);
  if(d.ok){ toast('reference added'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'upload failed'); } }
function dropRef(ev,sid){ ev.preventDefault(); document.getElementById('drop-'+sid).classList.remove('over');
  const f=ev.dataTransfer.files[0]; if(f) uploadRef(sid,f); }
async function removeRef(sid,name){ const {ok}=await post('/api/shot/'+sid+'/reference/remove',{name:name});
  if(ok){ toast('reference removed'); setTimeout(()=>location.reload(),300);} else { toast('remove failed'); } }

async function uploadImage(sid,file){ if(!file) return; toast('uploading image\\u2026');
  const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/shot/'+sid+'/image',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('image uploaded & selected'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'upload failed'); } }
function dropImage(ev,sid){ ev.preventDefault(); document.getElementById('imgdrop-'+sid).classList.remove('over');
  const f=ev.dataTransfer.files[0]; if(f) uploadImage(sid,f); }

async function uploadClip(sid,file){ if(!file) return; toast('importing clip (normalizing\\u2026)'); const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/shot/'+sid+'/clip',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('hero clip imported ('+d.duration+'s)'); setTimeout(()=>location.reload(),500);} else { toast(d.error||'import failed'); } }
function dropClip(ev,sid){ ev.preventDefault(); document.getElementById('clipdrop-'+sid).classList.remove('over');
  const f=ev.dataTransfer.files[0]; if(f) uploadClip(sid,f); }

async function uploadFrame(file){ if(!file) return; toast('uploading frame\\u2026'); const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/render/reference',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('frame reference set'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'upload failed'); } }
function dropFrame(ev){ ev.preventDefault(); document.getElementById('framedrop').classList.remove('over');
  const f=ev.dataTransfer.files[0]; if(f) uploadFrame(f); }
async function clearFrame(){ const {ok}=await post('/api/render/reference/clear'); if(ok){ toast('frame cleared'); setTimeout(()=>location.reload(),300);} }

let chat=[];
function logMsg(role,text){ const l=document.getElementById('chatlog');
  const d=document.createElement('div'); 
  d.className = 'p-3 rounded-lg max-w-[85%] ' + (role==='user' ? 'self-end bg-zinc-800 text-zinc-100' : 'self-start bg-amber-500/10 border border-amber-500/20 text-amber-300 white-space-pre-wrap');
  
  const header = document.createElement('div');
  header.className = 'text-[9px] text-zinc-500 font-semibold mb-1 uppercase tracking-wider';
  header.textContent = role==='user' ? 'You' : 'Vesper';
  d.appendChild(header);
  
  const content = document.createElement('div');
  content.className = 'text-sm';
  content.textContent = text;
  d.appendChild(content);
  
  l.appendChild(d); l.scrollTop=l.scrollHeight; }

async function chatSend(){ const inp=document.getElementById('chatinput'); const text=inp.value.trim(); if(!text) return;
  inp.value=''; logMsg('user',text); chat.push({role:'user',content:text});
  const ch = document.getElementById('project_channel').value;
  const {ok,data}=await post('/chat/develop',{messages:chat, channel:ch});
  if(ok){ logMsg('assistant',data.reply); chat.push({role:'assistant',content:data.reply}); }
  else { logMsg('assistant','[error] '+(data.error||'failed')); } }

async function scriptFromChat(){
  if(!chat.length){ toast('chat with Vesper first'); return; }
  if(!confirm('\\u26A0 DESTRUCTIVE: turn this conversation into a NEW storyboard, OVERWRITING the active project (all shot text, knobs, chosen drafts, uploaded reference links). Continue?')) return;
  const beats=document.getElementById('gen_beats').value;
  const ch=document.getElementById('project_channel').value;
  toast('writing script from chat\\u2026');
  const {ok,data}=await post('/api/script/from_chat',{messages:chat,beats:beats||null,channel:ch});
  if(ok){ toast('scripted '+data.shots+' beats from chat'); setTimeout(()=>location.reload(),600); }
  else { alert('Failed:\\n'+(data.error||'')); } }

async function genStoryboard(){ const topic=document.getElementById('gen_topic').value.trim(); if(!topic){ toast('enter a topic'); return; }
  if(!confirm('\\u26A0 DESTRUCTIVE: Draft Storyboard will OVERWRITE the active project.\\n\\n'
    +'A fresh AI draft replaces EVERYTHING in this manifest:\\n'
    +'  \\u2022 all shot text (narration, scene, style_medium)\\n'
    +'  \\u2022 the generation knobs (guidance / cfg / steps / negative)\\n'
    +'  \\u2022 chosen drafts and per-shot uploaded reference links\\n\\n'
    +'This cannot be undone. Continue?')) return;
  const beats=document.getElementById('gen_beats').value;
  const ch=document.getElementById('project_channel').value;
  toast('drafting…'); const {ok,data}=await post('/api/script/generate',{topic:topic,beats:beats||null,channel:ch});
  if(ok){ toast('drafted '+data.shots+' beats'); setTimeout(()=>location.reload(),600); } else { alert('Draft failed:\\n'+(data.error||'')); } }

async function lockScript(){ const {ok,data}=await post('/api/script/lock');
  if(ok){ toast('script locked'); setTimeout(()=>location.reload(),500); } else { alert('Cannot lock:\\n'+(data.error||'')); } }

async function assemble(stage,btn){ btn.disabled=true;
  const {ok,data}=await post('/api/assemble/'+stage,{});
  if(!ok){ toast(data.error||'could not start'); btn.disabled=false; return; }
  toast(stage+' started'); pollAssemble(); }
let _lastStatus={};
async function pollAssemble(){ let r; try{ r=await fetch('/api/assemble/status'); }catch(e){ return; }
  const d=await r.json(); let running=false, justFinished=false;
  for(const [k,v] of Object.entries(d.jobs||{})){
    const el=document.getElementById('st-'+k), b=document.getElementById('btn-'+k), sp=document.getElementById('spinner-'+k);
    if(el){ 
      el.textContent=v.status+(v.status==='error'?' \\u2014 check terminal':'');
      if(v.status==='error') { el.className="text-xs text-rose-500 ml-4 font-mono font-semibold"; }
      else if(v.status==='done') { el.className="text-xs text-emerald-500 ml-4 font-mono font-semibold"; }
      else { el.className="text-xs text-amber-500 ml-4 font-mono font-semibold animate-pulse"; }
    }
    if(b){ 
      b.disabled=(v.status==='running'); 
      if(v.status==='running') {
        b.classList.add('border-amber-500/50');
      } else {
        b.classList.remove('border-amber-500/50');
      }
    }
    if(sp) {
      if(v.status==='running') { sp.classList.remove('hidden'); }
      else { sp.classList.add('hidden'); }
    }
    if(v.status==='running') running=true;
    if(_lastStatus[k]==='running' && v.status!=='running') justFinished=true;
    _lastStatus[k]=v.status; }
  if(running) setTimeout(pollAssemble,2500);
  else if(justFinished) setTimeout(()=>location.reload(),800); }  // show new clips/preview
if(document.getElementById('btn-narration')) pollAssemble();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/api/debug/gcs")
def debug_gcs():
    import os
    gcs_parent = Path("/gcs").resolve()
    all_manifests = []
    if gcs_parent.exists():
        for dirpath, dirnames, filenames in os.walk(gcs_parent):
            if "assets" in dirpath or "references" in dirpath or "source" in dirpath:
                continue
            if "storyboard_manifest.json" in filenames:
                all_manifests.append(Path(dirpath) / "storyboard_manifest.json")
                
    manifest_infos = {}
    for p in all_manifests:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            manifest_infos[str(p)] = {
                "title": data.get("title"),
                "channel": data.get("channel"),
                "shots_count": len(data.get("shots", [])),
                "shots_slice": [s.get("scene_id") for s in data.get("shots", [])[:5]]
            }
        except Exception as e:
            manifest_infos[str(p)] = f"Error: {e}"
            
    return jsonify({
        "all_manifests_found": [str(x) for x in all_manifests],
        "manifest_infos": manifest_infos,
        "active_manifest": str(_state["manifest"]),
        "scanned_projects": _scan_projects()
    })


@app.get("/")
def index():
    sb = _load()
    reg = _ref_registry()
    shot_refs = {
        s.scene_id: [{"name": n, "file": _ref_file(n, reg)} for n in s.references]
        for s in sb.shots
    }
    # finished outputs to show: per-shot rendered clips + the assembled preview
    ep = config.episode_paths(sb.title)

    def _render_url(p):
        return "/render/" + str(p.relative_to(config.RENDER_DIR)).replace("\\", "/")

    shot_clips = {}
    for s in sb.shots:
        clip = ep["render"] / f"{s.scene_id}.mp4"
        shot_clips[s.scene_id] = _render_url(clip) if clip.exists() else None
    preview = ep["render"] / "_preview.mp4"
    preview_url = _render_url(preview) if preview.exists() else None
    fcpxml_ready = (config.ROOT / f"{ep['slug']}.fcpxml").exists()

    from .assets import NEGATIVE_PROMPT
    return render_template_string(
        PAGE, sb=sb, tiers=TIER_LABEL, paid=_paid_count(sb),
        projects=_scan_projects(), render=sb.render, shot_refs=shot_refs,
        default_negative=NEGATIVE_PROMPT, backends=BACKENDS, video_backends=VIDEO_BACKENDS,
        heroes=sum(1 for s in sb.shots if getattr(s, "flow_hero", False)),
        shot_clips=shot_clips, preview_url=preview_url,
        fcpxml_ready=fcpxml_ready, ep_slug=ep["slug"], media_url=_media_url,
        motion_suggest={s.scene_id: _suggest_motion_prompt(s) for s in sb.shots},
    )


def _media_url(path_str: str | None) -> str:
    if not path_str:
        return ""
    p = str(path_str).replace("\\", "/").strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    clean = p.lstrip("/")
    return f"/media/{clean}"


@app.get("/media/<path:filepath>")
def serve_media(filepath: str):
    clean = filepath.replace("\\", "/").lstrip("/")
    active_dir = config.MANIFEST_PATH.parent

    candidates = [
        WORKSPACE_ROOT / clean,
        active_dir / clean,
        config.ASSETS / clean,
        config.REFERENCES_DIR / clean,
        Path("/") / clean,
    ]

    for cand in candidates:
        try:
            res = cand.resolve()
            if res.exists() and res.is_file():
                resp = send_from_directory(str(res.parent), res.name)
                resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
                return resp
        except Exception:
            pass

    parts = clean.split("/")
    filename = parts[-1]
    if len(parts) >= 2:
        scene_id = parts[-2]
        try:
            cand = (config.ASSETS / scene_id / filename).resolve()
            if cand.exists() and cand.is_file():
                resp = send_from_directory(str(cand.parent), cand.name)
                resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
                return resp
        except Exception:
            pass

    return jsonify(error=f"Media not found: {filepath}"), 404


def _resolve_local_image_file(path_str: str | Path | None, scene_id: str | None = None) -> Path | None:
    if not path_str:
        return None
    p_raw = str(path_str).replace("\\", "/").strip()
    p_clean = p_raw.lstrip("/")
    active_dir = config.MANIFEST_PATH.parent

    candidates = [
        Path(p_raw),
        WORKSPACE_ROOT / p_clean,
        active_dir / p_clean,
        config.ASSETS / p_clean,
        config.REFERENCES_DIR / p_clean,
        Path("/") / p_clean,
    ]

    parts = p_clean.split("/")
    filename = parts[-1]
    sid = scene_id or (parts[-2] if len(parts) >= 2 else None)
    if sid:
        candidates.append(config.ASSETS / sid / filename)
        candidates.append(active_dir / sid / filename)
        candidates.append(WORKSPACE_ROOT / "assets" / sid / filename)

    for cand in candidates:
        try:
            res = cand.resolve()
            if res.exists() and res.is_file():
                return res
        except Exception:
            pass
    return None


@app.get("/assets/<path:filename>")
def asset_legacy(filename: str):
    return serve_media(f"assets/{filename}")


@app.get("/assets/<scene>/<path:filename>")
def asset(scene: str, filename: str):
    return serve_media(f"assets/{scene}/{filename}")


@app.get("/references/<path:filename>")
def reference_file(filename: str):
    return serve_media(f"references/{filename}")


@app.get("/render/<path:filename>")
def render_file(filename: str):
    return serve_media(f"render/{filename}")


@app.post("/api/project/select")
def select_project():
    data = request.get_json(force=True) or {}
    rel = (data.get("rel") or "").strip()
    
    # Try resolving as an absolute path first
    target = Path(rel).resolve()
    if target.name != "storyboard_manifest.json" or not target.exists():
        # Fall back to resolving relative to WORKSPACE_ROOT
        target = (WORKSPACE_ROOT / rel).resolve()
        
    if target.name != "storyboard_manifest.json" or not target.exists():
        return jsonify(ok=False, error="not a valid project manifest"), 400
    
    _set_active_manifest_path(target)
    config.set_active_manifest(target)
    
    try:
        rel_to_root = target.relative_to(WORKSPACE_ROOT.resolve())
    except ValueError:
        rel_to_root = target

    return jsonify(ok=True, active=str(rel_to_root).replace("\\", "/"))


@app.post("/api/render")
def update_render():
    sb = _load()
    data = request.get_json(force=True) or {}
    r = sb.render
    if "backend" in data:
        be_str = str(data["backend"])
        backends = [b.strip() for b in be_str.split(",") if b.strip()]
        if backends and all(b in ALLOWED_BACKENDS for b in backends):
            r.backend = be_str
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
    _save(sb)
    return jsonify(ok=True, render=asdict(r))


def _safe_rel_path(dest: Path) -> str:
    try:
        return str(dest.relative_to(config.ROOT)).replace("\\", "/")
    except ValueError:
        return str(dest).replace("\\", "/").lstrip("/")


@app.post("/api/render/reference")
def set_reference_image():
    """Upload/replace the project's GLOBAL frame reference (nano2 conditions on it)."""
    import fal_client

    sb = _load()
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="no file uploaded"), 400
    try:
        config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        ext = secure_filename(file.filename).rpartition(".")[2] or "png"
        dest = config.REFERENCES_DIR / f"global_frame.{ext}"
        file.save(str(dest))
        url = fal_client.upload_file(str(dest))  # needs FAL_KEY
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    sb.render.reference_image = _safe_rel_path(dest)
    sb.render.reference_image_url = url
    _save(sb)
    return jsonify(ok=True, path=sb.render.reference_image)


@app.post("/api/render/reference/clear")
def clear_reference_image():
    sb = _load()
    sb.render.reference_image = ""
    sb.render.reference_image_url = ""
    _save(sb)
    return jsonify(ok=True)


@app.post("/api/shot/<scene_id>")
def update_shot(scene_id: str):
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    data = request.get_json(force=True) or {}

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
        # Tier C needs a video model for the gate; other tiers clear it.
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

    _save(sb)
    return jsonify(ok=True, paid_count=_paid_count(sb))


@app.post("/api/shot/<scene_id>/reference")
def add_reference(scene_id: str):
    """Drag-drop upload: save an image and append it to the shot's reference list."""
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="no file uploaded"), 400

    fname = secure_filename(f"{scene_id}_{file.filename}")
    config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.REFERENCES_DIR / fname
    file.save(str(dest))

    name = dest.stem
    reg = _ref_registry()
    reg[name] = {"files": [fname]}  # cached fal urls are re-derived on demand
    _save_ref_registry(reg)
    if name not in shot.references:
        shot.references.append(name)
    _save(sb)
    return jsonify(ok=True, name=name, file=fname, references=shot.references)


@app.post("/api/shot/<scene_id>/image")
def add_image(scene_id: str):
    """Upload a finished image made outside the pipeline as a draft for this shot.

    Saves it beside any generated variations, appends it to ``draft_variations``,
    and auto-selects it (``chosen_variation`` + ``draft_image``) since it was made
    on purpose. No fal call — free, and works even if the shot has no drafts yet.
    """
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="no file uploaded"), 400

    dest_dir = config.ASSETS / scene_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = secure_filename(file.filename) or "image.png"
    stem, _, ext = base.rpartition(".")
    stem, ext = (stem or base), (ext or "png")
    n = 0
    while (dest_dir / f"upload_{n}_{stem}.{ext}").exists():
        n += 1
    dest = dest_dir / f"upload_{n}_{stem}.{ext}"
    file.save(str(dest))

    rel = _safe_rel_path(dest)
    shot.draft_variations.append(rel)
    shot.chosen_variation = len(shot.draft_variations) - 1
    shot.draft_image = rel
    _save(sb)
    return jsonify(ok=True, path=rel, chosen=shot.chosen_variation,
                   variations=len(shot.draft_variations))


@app.post("/api/shot/<scene_id>/reference/remove")
def remove_reference(scene_id: str):
    """Unlink a reference name from a shot (leaves the file/registry entry intact)."""
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if name not in shot.references:
        return jsonify(ok=False, error="reference not on this shot"), 400
    shot.references.remove(name)
    _save(sb)
    return jsonify(ok=True, references=shot.references)


@app.post("/api/shot/<scene_id>/clip")
def add_clip(scene_id: str):
    """Import a finished hero video (Veo/Flow) as this shot's render clip.

    Normalizes to the local render format (1280x720, 24fps, silent H.264) so it
    drops straight into the preview concat and the DaVinci timeline, fits the shot
    duration to the clip, and marks the shot so a future render won't overwrite it.
    """
    import subprocess
    import tempfile

    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="no file uploaded"), 400

    ep = config.episode_paths(sb.title)
    ep["render"].mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / secure_filename(f"heroin_{scene_id}_{file.filename}")
    file.save(str(tmp))
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
        return jsonify(ok=False, error=f"could not normalize clip: {exc}"), 500
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    from . import timeline
    dur = timeline._probe_seconds(dest)
    if shot.camera and dur > 0:
        shot.camera.duration = round(dur, 2)
    shot.hero_clip = True
    _save(sb)
    return jsonify(ok=True, duration=round(dur, 2),
                   path=_safe_rel_path(dest))


@app.post("/api/regenerate/<scene_id>")
def regenerate(scene_id: str):
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)
    # Spend guard: stop runaway paid calls before touching the fal path.
    if _regen_count["n"] >= REGEN_LIMIT:
        return jsonify(
            ok=False,
            error=f"Regenerate limit reached for this session ({REGEN_LIMIT}). "
                  f"Restart the server or raise STUDIO_REGEN_LIMIT to continue.",
        ), 429

    from . import assets  # lazy: only import the fal path when actually used

    data = request.get_json(silent=True) or {}
    backend = (data.get("backend") or getattr(sb.render, "backend", None)
               or assets.DEFAULT_BACKEND)
    backends = [b.strip() for b in backend.split(",") if b.strip()]
    if not backends or any(b not in ALLOWED_BACKENDS for b in backends):
        backend = assets.DEFAULT_BACKEND
    n = int(data.get("n", request.args.get("n", 3)))
    try:
        assets.generate_for_shot(shot, n, backend=backend, render=sb.render)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    _regen_count["n"] += 1
    _save(sb)
    return jsonify(ok=True, variations=shot.draft_variations, backend=backend,
                   regen_used=_regen_count["n"], regen_limit=REGEN_LIMIT)


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


def set_active_video_clip(sb, shot, video_rel_path, out_dir):
    import shutil
    shot.video_clip = video_rel_path
    src_path = WORKSPACE_ROOT / video_rel_path
    dest_path = out_dir / f"{shot.scene_id}.mp4"
    if src_path.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        try:
            frame_out_path = config.ASSETS / shot.scene_id / f"final_frame_{shot.scene_id}.png"
            from . import assets
            assets.extract_final_frame(dest_path, frame_out_path)
        except Exception as e:
            print(f"Error extracting final frame for {shot.scene_id}: {e}")


def create_claude_message(client, model, max_tokens, system, messages):
    import anthropic
    models_to_try = [model]
    fallbacks = [
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-4-6"
    ]
    for fb in fallbacks:
        if fb not in models_to_try:
            models_to_try.append(fb)

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
            exc_str = str(exc).lower()
            if "not_found" in exc_str or "not found" in exc_str or "404" in exc_str:
                last_exc = exc
                continue
            raise exc
    if last_exc:
        raise last_exc


@app.post("/api/shot/<scene_id>/chat")
def shot_chat(scene_id: str):
    """Refine image and video prompts for a specific shot using conversation."""
    import anthropic
    from . import script

    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)

    try:
        config.require_for("script")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400

    data = request.get_json(force=True) or {}
    messages = data.get("messages")
    if not messages:
        return jsonify(ok=False, error="empty messages"), 400

    current_motion_prompt = shot.motion_prompt or _suggest_motion_prompt(shot)

    system_prompt = f"""You are Vesper, a storyboard prompt specialist. You are assisting the director in refining the image prompt (used for still image generation) and/or the video motion prompt (used for video generation) for beat {scene_id}.

CURRENT VALUES FOR BEAT {scene_id}:
- Narration (words spoken): "{shot.narration}"
- Style Medium: "{shot.style_medium}"
- Image Prompt: "{shot.prompt}"
- Video Motion Prompt: "{current_motion_prompt}"

The user will describe issues with the current renders or request changes (e.g. adding details, changing lighting, camera movement, speed).

Your job:
1. Converse naturally and professionally as Vesper. Analyze the feedback and discuss how to adjust the prompts.
2. Propose refined prompt strings. Keep the prompt descriptions concise, vivid, and aligned with the "Style Medium" and historical context.
3. You MUST end your response with a JSON code block containing the proposed refined prompts, wrapped exactly in ```json ... ``` code blocks.
4. If one of the prompts does not need changes, keep it the same as the current value.

The JSON structure at the end of your response MUST match this format:
```json
{{
  "refined_prompt": "Refined Image Prompt here",
  "refined_motion_prompt": "Refined Video Motion Prompt here"
}}
```

Keep your conversation concise and helpful. Explain exactly what changes you made and why.
"""

    try:
        client = anthropic.Anthropic()
        resp = create_claude_message(
            client=client,
            model=script.DEFAULT_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

        import re
        import json
        refined_prompt = None
        refined_motion_prompt = None

        json_match = re.search(r"```json\s*(.*?)\s*```", reply, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1).strip())
                refined_prompt = parsed.get("refined_prompt")
                refined_motion_prompt = parsed.get("refined_motion_prompt")
            except Exception:
                pass

        display_reply = reply
        if json_match:
            display_reply = reply[:json_match.start()].strip()

    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500

    return jsonify(
        ok=True,
        reply=display_reply,
        refined_prompt=refined_prompt,
        refined_motion_prompt=refined_motion_prompt
    )


@app.post("/api/shot/<scene_id>/apply_chat_prompts")
def apply_chat_prompts(scene_id: str):
    """Save the refined prompts suggested by Claude to this shot."""
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)

    data = request.get_json(force=True) or {}
    refined_prompt = data.get("refined_prompt")
    refined_motion_prompt = data.get("refined_motion_prompt")

    if refined_prompt:
        shot.prompt = refined_prompt
    if refined_motion_prompt:
        shot.motion_prompt = refined_motion_prompt

    _save(sb)
    return jsonify(ok=True, prompt=shot.prompt, motion_prompt=shot.motion_prompt)


@app.post("/api/shot/<scene_id>/edit_image/<int:var_idx>")
def edit_shot_image(scene_id: str, var_idx: int):
    """Generate fine-tuned image variations starting from an existing draft variation."""
    import fal_client
    from . import assets

    sb = _load()
    shot = _find(sb, scene_id)
    if not shot or not shot.draft_variations:
        abort(404)

    if var_idx < 0 or var_idx >= len(shot.draft_variations):
        return jsonify(ok=False, error="Index out of range"), 400

    data = request.get_json(force=True) or {}
    edit_prompt = (data.get("prompt") or "").strip()
    if not edit_prompt:
        return jsonify(ok=False, error="Empty edit prompt"), 400

    # Spend guard
    if _regen_count["n"] >= REGEN_LIMIT:
        return jsonify(ok=False, error=f"Regenerate limit reached for this session ({REGEN_LIMIT})."), 429

    backend = (data.get("backend") or getattr(sb.render, "backend", None) or assets.DEFAULT_BACKEND)

    # 1. Resolve local file path
    rel_path = shot.draft_variations[var_idx]
    local_path = _resolve_local_image_file(rel_path, scene_id=scene_id)
    if not local_path or not local_path.exists():
        return jsonify(ok=False, error=f"Base image variation not found on disk: {rel_path}"), 400

    try:
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

        _regen_count["n"] += 1
        _save(sb)

        return jsonify(ok=True, variations=shot.draft_variations, chosen=shot.chosen_variation)

    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/api/shot/<scene_id>/delete_image/<int:var_idx>")
def delete_shot_image(scene_id: str, var_idx: int):
    """Delete a specific still image variation from the list and GCS/disk."""
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot or not shot.draft_variations:
        abort(404)
    if var_idx < 0 or var_idx >= len(shot.draft_variations):
        return jsonify(ok=False, error="Index out of range"), 400

    rel_path = shot.draft_variations[var_idx]
    full_path = _resolve_local_image_file(rel_path, scene_id=scene_id)
    if full_path and full_path.exists():
        try:
            full_path.unlink()
        except Exception as e:
            print(f"Error deleting image file {full_path}: {e}")

    shot.draft_variations.pop(var_idx)

    # Re-evaluate chosen/active draft pointers
    if not shot.draft_variations:
        shot.draft_image = ""
        shot.chosen_variation = 0
    else:
        if shot.chosen_variation == var_idx:
            shot.chosen_variation = 0
            shot.draft_image = shot.draft_variations[0]
        elif shot.chosen_variation > var_idx:
            shot.chosen_variation -= 1
            shot.draft_image = shot.draft_variations[shot.chosen_variation]

    _save(sb)
    return jsonify(ok=True, variations=shot.draft_variations)


@app.post("/api/shot/<scene_id>/delete_video/<int:var_idx>")
def delete_shot_video_variation(scene_id: str, var_idx: int):
    """Delete a specific video variation from the list and GCS/disk."""
    sb = _load()
    shot = _find(sb, scene_id)
    if not shot or not shot.video_variations:
        abort(404)
    if var_idx < 0 or var_idx >= len(shot.video_variations):
        return jsonify(ok=False, error="Index out of range"), 400

    rel_path = shot.video_variations[var_idx]
    full_path = _resolve_local_image_file(rel_path, scene_id=scene_id)
    if full_path and full_path.exists():
        try:
            full_path.unlink()
        except Exception as e:
            print(f"Error deleting video file {full_path}: {e}")

    shot.video_variations.pop(var_idx)

    # If we deleted the active clip, clear it or assign a new fallback
    if shot.video_clip == rel_path:
        if shot.video_variations:
            new_path = shot.video_variations[0]
            out_dir = config.episode_paths(sb.title)["render"]
            set_active_video_clip(sb, shot, new_path, out_dir)
        else:
            shot.video_clip = ""
            out_dir = config.episode_paths(sb.title)["render"]
            render_path = out_dir / f"{shot.scene_id}.mp4"
            if render_path.exists():
                try:
                    render_path.unlink()
                except Exception:
                    pass

    _save(sb)
    return jsonify(ok=True, variations=shot.video_variations)


@app.get("/api/test_anthropic")
def test_anthropic_models():
    """Diagnostic route to test all Claude models and report exact API response errors."""
    import anthropic
    
    raw_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
    clean_key = raw_key.strip()
    
    # Check for hidden characters in key repr
    key_repr = repr(raw_key)
    has_newline = "\n" in raw_key or "\r" in raw_key
    has_spaces = raw_key.startswith(" ") or raw_key.endswith(" ")
    
    # Test standard environment key
    client_env = anthropic.Anthropic()
    
    # Test cleaned (stripped) key explicitly
    client_clean = anthropic.Anthropic(api_key=clean_key)
    
    models = [
        "claude-sonnet-5",
        "claude-sonnet-4.6",
        "claude-opus-4-8",
        "claude-fable-5",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-latest"
    ]
    
    results = {}
    
    # Test with standard environment client
    results["env_client_tests"] = {}
    for m in models:
        try:
            resp = client_env.messages.create(
                model=m,
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}]
            )
            results["env_client_tests"][m] = {"status": "SUCCESS", "response": resp.id}
        except Exception as exc:
            results["env_client_tests"][m] = {"status": "FAILED", "error": str(exc)}
            
    # Test with stripped key client
    results["clean_client_tests"] = {}
    for m in models:
        try:
            resp = client_clean.messages.create(
                model=m,
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}]
            )
            results["clean_client_tests"][m] = {"status": "SUCCESS", "response": resp.id}
        except Exception as exc:
            results["clean_client_tests"][m] = {"status": "FAILED", "error": str(exc)}
            
    results["_key_diagnostics"] = {
        "raw_length": len(raw_key),
        "clean_length": len(clean_key),
        "has_newline": has_newline,
        "has_spaces": has_spaces,
        "prefix": raw_key[:12] if len(raw_key) >= 12 else raw_key,
        "suffix": raw_key[-4:] if len(raw_key) >= 4 else raw_key,
        "starts_with_sk_ant": clean_key.startswith("sk-ant-"),
        "key_repr_visual": key_repr,
        "env_vars_dump": {k: v for k, v in os.environ.items() if any(x in k.upper() for x in ["ANTHROPIC", "API", "URL", "PROXY", "BASE", "HOST", "GCP", "SECRET"])}
    }
    return jsonify(results)


@app.post("/api/shot/<scene_id>/generate_video")
def generate_shot_video(scene_id: str):
    """Generate a single paid Fal.ai video clip for this specific shot."""
    import fal_client
    from . import assets

    sb = _load()
    shot = _find(sb, scene_id)
    if not shot:
        abort(404)

    config.require_for("video")
    data = request.get_json(silent=True) or {}
    video_model_key = data.get("video_model") or getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
    shot.video_model = video_model_key
    shot.motion_type = MotionType.AI_VIDEO

    model_endpoint = resolve_video_model_endpoint(video_model_key)

    # Resolve or auto-generate still image draft
    local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
    if not local_image_path or not local_image_path.exists():
        print(f"Still image draft not found for {shot.scene_id}, generating still drafts automatically...")
        try:
            assets.generate_for_shot(shot, n=3, backend=sb.render.backend, render=sb.render)
            shot.chosen_variation = 0
            shot.draft_image = shot.draft_variations[0]
            _save(sb)
            local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
        except Exception as exc:
            return jsonify(ok=False, error=f"Could not generate still image draft: {exc}"), 400

    if not local_image_path or not local_image_path.exists():
        return jsonify(ok=False, error=f"Still image draft file not found on disk: {shot.draft_image}"), 400

    out_dir = config.episode_paths(sb.title)["render"]
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        public_image_url = fal_client.upload_file(str(local_image_path))

        # Target length determined by Claude in the script stage
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

        # Check if extending previous video segment natively
        chaining_mode = getattr(sb.render, "video_chaining", "native_extend")
        shots_list = sb.shots
        shot_idx = next((i for i, s in enumerate(shots_list) if s.scene_id == scene_id), 0)
        if shot_idx > 0:
            prev_shot = shots_list[shot_idx - 1]
            prev_video_path = out_dir / f"{prev_shot.scene_id}.mp4"
            if chaining_mode == "native_extend" and prev_video_path.exists() and video_model_key in ("seedance_2_0", "luma_dream_machine", "hunyuan_video"):
                public_video_url = fal_client.upload_file(str(prev_video_path))
                arguments["video_url"] = public_video_url

        result = fal_client.subscribe(
            model_endpoint,
            arguments=arguments,
            with_logs=True
        )

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

        _save(sb)
        return jsonify(ok=True, video_path=f"/render/{scene_id}.mp4", video_model=video_model_key)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/api/script/generate")
def script_generate():
    """Draft a full structured storyboard from a topic via src/script.py (Claude)."""
    from . import script

    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify(ok=False, error="topic is required"), 400
    beats = data.get("beats")
    channel = data.get("channel") or "bestiary"
    try:
        sb = script.generate_script(topic, num_beats=int(beats) if beats else None, channel=channel)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    _save(sb)
    return jsonify(ok=True, shots=len(sb.shots), title=sb.title,
                   cultural_origin=sb.cultural_origin)


@app.post("/api/script/from_chat")
def script_from_chat():
    """Turn the Vesper develop-chat conversation into a full structured storyboard."""
    from . import script

    data = request.get_json(force=True) or {}
    messages = data.get("messages") or []
    if not messages:
        return jsonify(ok=False, error="no conversation yet — chat with Vesper first"), 400
    beats = data.get("beats")
    channel = data.get("channel") or "bestiary"
    try:
        sb = script.generate_script_from_messages(messages, num_beats=int(beats) if beats else None, channel=channel)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    _save(sb)
    return jsonify(ok=True, shots=len(sb.shots), title=sb.title,
                   cultural_origin=sb.cultural_origin)


@app.post("/api/script/lock")
def script_lock():
    """The script gate: validate the beats and lock the script (src/script.py)."""
    from . import script

    sb = _load()
    try:
        sb = script.lock_script(sb)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400
    _save(sb)
    return jsonify(ok=True, locked=sb.script_locked, shots=len(sb.shots))


@app.post("/api/project/meta")
def update_project_meta():
    sb = _load()
    data = request.get_json(force=True) or {}
    if "title" in data:
        sb.title = str(data["title"]).strip()
    if "channel" in data:
        sb.channel = str(data["channel"]).strip()
    _save(sb)
    return jsonify(ok=True, title=sb.title, channel=getattr(sb, "channel", "bestiary"))


@app.post("/chat/develop")
def chat_develop():
    """Proxy a chat turn to Claude using Vesper's documentary system prompt."""
    from . import script
    import anthropic

    try:
        config.require_for("script")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400

    data = request.get_json(force=True) or {}
    messages = data.get("messages")
    if not messages:
        text = (data.get("input") or "").strip()
        if not text:
            return jsonify(ok=False, error="empty input"), 400
        messages = [{"role": "user", "content": text}]

    channel = data.get("channel") or "bestiary"
    system_prompt = script.get_system_prompt(channel)
    try:
        client = anthropic.Anthropic()
        resp = create_claude_message(
            client=client,
            model=script.DEFAULT_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, reply=reply)


@app.post("/api/approve")
def approve():
    """The gate: block until every beat has a chosen image, then approve."""
    sb = _load()
    missing = [s.scene_id for s in sb.shots if not s.draft_image]
    if not sb.shots:
        return jsonify(ok=False, error="No beats to approve."), 400
    if missing:
        return jsonify(ok=False, error="These beats have no chosen image:", scenes=missing), 400

    for s in sb.shots:
        s.approved = True
        if s.motion_type == MotionType.AI_VIDEO and not s.video_model:
            s.video_model = DEFAULT_VIDEO_MODEL
    sb.storyboard_approved = True
    _save(sb)
    return jsonify(ok=True, gate_cleared=sb.gate_cleared(),
                   paid=[s.scene_id for s in sb.paid_shots()])


@app.post("/api/project/new")
def new_project():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    channel = (data.get("channel") or "bestiary").strip()
    
    # If GCS assets folder exists, create new projects inside the channel folder so they persist!
    gcs_root = Path("/gcs/assets").resolve()
    if gcs_root.exists():
        scan_root = gcs_root / channel
        scan_root.mkdir(parents=True, exist_ok=True)
    else:
        scan_root = WORKSPACE_ROOT.resolve()

    if not name:
        n = 1
        while (scan_root / f"project_{n}").exists():
            n += 1
        name = f"project_{n}"
    
    from werkzeug.utils import secure_filename
    name = secure_filename(name)
    if not name:
        name = "untitled_project"
        
    proj_dir = scan_root / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    
    manifest_file = proj_dir / "storyboard_manifest.json"
    
    sb = Storyboard(title=name, channel=channel)
    save(sb, manifest_file)
    
    _set_active_manifest_path(manifest_file)
    config.set_active_manifest(manifest_file)
    
    return jsonify(ok=True, rel=str(manifest_file.resolve()).replace("\\", "/"))


def generate_fal_and_render(sb: Storyboard) -> None:
    import fal_client
    from . import assets, motion
    
    config.require_for("assets")
    
    IMAGE_MODEL_MAP = {
        "flux_2_max": "flux_2_max",
        "flux_2_pro": "flux_2_pro",
        "flux_1_1_pro_ultra": "flux_1_1_pro_ultra",
        "flux_1_dev_turbo": "flux_1_dev_turbo",
        "ideogram_4": "ideogram_4",
        "ideogram_4_instant": "ideogram_4_instant",
    }
    
    VIDEO_MODEL_MAP = {
        "seedance_2_0": "fal-ai/bytedance/seedance-2.0/image-to-video",
        "veo_3_1": "fal-ai/veo3.1/image-to-video",
        "kling_2_5_turbo_pro": "fal-ai/kling-video/v3/image-to-video",
        "wan_2_7": "fal-ai/wan/v2.7/image-to-video",
        "hunyuan_video": "fal-ai/hunyuan-video/image-to-video",
        "luma_dream_machine": "fal-ai/luma-dream-machine/ray-2/image-to-video",
    }
    
    out_dir = config.episode_paths(sb.title)["render"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Refresh config manifest path dynamically in case it's in a subdirectory
    config.set_active_manifest(_state["manifest"])
    
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
            _save(sb)
            
        is_ai = (shot.motion_type == MotionType.AI_VIDEO)
        
        if is_ai:
            video_key = getattr(shot, "video_model", None) or getattr(sb.render, "video_model", "seedance_2_0")
            model_endpoint = resolve_video_model_endpoint(video_key)
            print(f"Generating paid video for {shot.scene_id} using {model_endpoint} (chaining: {chaining_mode})...")
            # Target length determined by Claude in the script stage
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

            # Option A: Native Video Extend if supported and prev clip exists
            if (chaining_mode == "native_extend" and prev_video_dest_path and prev_video_dest_path.exists()
                    and video_key in ("seedance_2_0", "luma_dream_machine", "hunyuan_video")):
                print(f"Native Video Extend: extending from previous segment {prev_video_dest_path.name}...")
                public_video_url = fal_client.upload_file(str(prev_video_dest_path))
                arguments["video_url"] = public_video_url
            else:
                # Option B: OpenCV Final Frame Chaining or initial draft still image
                if chaining_mode != "independent" and prev_extracted_frame and prev_extracted_frame.exists():
                    local_image_path = prev_extracted_frame
                    print(f"Continuous sequence flow: chaining from OpenCV final frame -> {local_image_path.name}")
                else:
                    local_image_path = _resolve_local_image_file(shot.draft_image, scene_id=shot.scene_id)
                    if not local_image_path or not local_image_path.exists():
                        print(f"Still image draft not found for {shot.scene_id}, generating still drafts automatically...")
                        try:
                            assets.generate_for_shot(shot, n=3, backend=sb.render.backend, render=sb.render)
                            shot.chosen_variation = 0
                            shot.draft_image = shot.draft_variations[0]
                            _save(sb)
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
            
            result = fal_client.subscribe(
                model_endpoint,
                arguments=arguments,
                with_logs=True
            )
            
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
            _save(sb)

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


@app.post("/api/assemble/<stage>")
def assemble(stage: str):
    """Kick off a back-half stage in the background for the active project."""
    sb = _load()
    if stage == "narration":
        if not sb.script_locked:
            return jsonify(ok=False, error="Lock the script first."), 400
        from . import audio

        def fn():
            audio.synthesize_narration(sb)
            changed = audio.sync_durations(sb)   # narration-led pacing (no VO overlap)
            _save(sb)
            print(f"Narration done; fitted {changed} shot duration(s) to the voiceover.")
            print("Re-run Render clips + Build preview so video matches the new lengths.")
    elif stage == "render":
        if not sb.storyboard_approved:
            return jsonify(ok=False, error="Approve the storyboard first."), 400
        fn = lambda: generate_fal_and_render(sb)  # noqa: E731
    elif stage == "preview":
        from . import timeline
        fn = lambda: timeline.build_preview(sb)  # noqa: E731
    elif stage == "timeline":
        from . import timeline
        fn = lambda: timeline.build(sb)  # noqa: E731
    else:
        abort(404)
    if _start_job(stage, fn):
        return jsonify(ok=True, stage=stage)
    return jsonify(ok=False, error=f"{stage} already running"), 409


@app.get("/api/assemble/status")
def assemble_status():
    with _jobs_lock:
        return jsonify(jobs={
            k: {"status": v["status"], "log": (v.get("log") or "")[-1500:]}
            for k, v in _jobs.items()
        })


def ensure_gcs_projects():
    gcs_root = Path("/gcs").resolve()
    if gcs_root.exists():
        # Bestiary project
        bestiary_dir = gcs_root / "bestiary"
        bestiary_dir.mkdir(parents=True, exist_ok=True)
        bestiary_manifest = bestiary_dir / "storyboard_manifest.json"
        
        # Calluses project
        calluses_dir = gcs_root / "calluses"
        calluses_dir.mkdir(parents=True, exist_ok=True)
        calluses_manifest = calluses_dir / "storyboard_manifest.json"
        
        # Copy Bestiary if missing or empty
        local_bestiary = Path(WORKSPACE_ROOT) / "storyboard_manifest.bestiary.json"
        if local_bestiary.exists():
            should_copy = True
            if bestiary_manifest.exists():
                try:
                    data = json.loads(bestiary_manifest.read_text(encoding="utf-8"))
                    if len(data.get("shots", [])) > 0:
                        should_copy = False
                except Exception:
                    pass
            if should_copy:
                bestiary_manifest.write_text(local_bestiary.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"Copied default bestiary manifest to {bestiary_manifest}")
                
        # Copy Calluses if missing or empty
        local_calluses = Path(WORKSPACE_ROOT) / "storyboard_manifest.calluses.json"
        if local_calluses.exists():
            should_copy = True
            if calluses_manifest.exists():
                try:
                    data = json.loads(calluses_manifest.read_text(encoding="utf-8"))
                    if len(data.get("shots", [])) > 0:
                        should_copy = False
                except Exception:
                    pass
            if should_copy:
                calluses_manifest.write_text(local_calluses.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"Copied default calluses manifest to {calluses_manifest}")


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    try:
        ensure_gcs_projects()
    except Exception as exc:
        print(f"Warning: Failed to ensure GCS project manifests: {exc}")
    print(f"Studio workspace: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run(debug=True)
