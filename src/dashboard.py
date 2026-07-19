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
                   send_file)
from werkzeug.utils import secure_filename

from . import config
from .manifest import MotionType, load, save

app = Flask(__name__)

# fal.ai 2026 Video Routing Roster
VIDEO_BACKENDS = {
    "veo-3.1": "Veo 3.1 (Cinematic 4K + Audio)",
    "seedance-2": "Seedance 2.0 (Multi-shot + Audio)",
    "wan-2.7": "Wan 2.7 (Budget B-Roll)",
    "kling-2.5": "Kling 2.5 Turbo Pro (Fast Motion)"
}
DEFAULT_VIDEO_MODEL = "veo-3.1"

TIER_LABEL = {"static": "A · still + FX ($0)", "parallax": "B · parallax ($0)",
              "ai_video": "C · AI video (paid)"}

# fal.ai 2026 Image Routing Roster
BACKENDS = {
    "flux-pro": "FLUX.2 [pro] (Cinematic, ~$0.045)",
    "flux-ultra": "FLUX 1.1 [pro] Ultra (2K Wide, ~$0.06)",
    "flux-turbo": "FLUX.1 [dev] Turbo (Fast Draft, ~$0.008)",
    "nano2": "Nano Banana 2 (Legacy, ~$0.15)"
}
ALLOWED_BACKENDS = {"flux-pro", "flux-ultra", "flux-turbo", "nano2", "flux-cfg", "nano", "flux", "flux-lora"}

# Root under which we look for sibling projects, and dirs we never descend into.
WORKSPACE_ROOT = config.ROOT
IGNORE_DIRS = {".venv", ".git", "__pycache__", "assets", "audio", "audio_pool",
               "lora_training", "render", "models", "sizzle", "intro", "references",
               "node_modules", "scripts", "src", "tmp", "temp", "output", "cache"}

# Active project manifest (this is a single-user local tool, so module state is fine).
_state = {"manifest": config.MANIFEST_PATH}

# Spend guard: cap paid image regenerations per server process. Raise via env.
REGEN_LIMIT = int(os.environ.get("STUDIO_REGEN_LIMIT", "50"))
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
    return load(_state["manifest"])


def _save(sb) -> None:
    save(sb, _state["manifest"])


def _find(sb, scene_id: str):
    return next((s for s in sb.shots if s.scene_id == scene_id), None)


def _paid_count(sb) -> int:
    return len(sb.paid_shots())


def _scan_projects() -> list[dict]:
    """Every dir under WORKSPACE_ROOT that holds a storyboard_manifest.json."""
    active = Path(_state["manifest"]).resolve()
    projects: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(WORKSPACE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        if "storyboard_manifest.json" in filenames:
            mf = Path(dirpath) / "storyboard_manifest.json"
            rel = mf.relative_to(WORKSPACE_ROOT)
            name = mf.parent.name if mf.parent != WORKSPACE_ROOT else WORKSPACE_ROOT.name
            projects.append({
                "name": name,
                "rel": str(rel).replace("\\", "/"),
                "active": mf.resolve() == active,
            })
    projects.sort(key=lambda p: p["rel"])
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
<html class="dark">
<head>
  <meta charset="utf-8">
  <title>{{ sb.title or "Untitled" }} — Studio Workspace</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            amber: { 400: '#ebba7a', 500: '#e0a458', 600: '#c2863c' },
            neutral: { 850: '#1a1a1a', 900: '#141414', 950: '#0a0a0a' }
          }
        }
      }
    }
  </script>
  <style>
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #333; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #555; }
    .toast-enter { opacity: 1 !important; transform: translate(-50%, 0) !important; }
  </style>
</head>
<body class="bg-neutral-950 text-neutral-300 font-sans antialiased m-0 flex flex-col min-h-screen">
  
  <header class="sticky top-0 z-30 bg-neutral-900/95 backdrop-blur-md border-b border-neutral-800 px-6 py-3 flex items-center gap-6 shadow-md">
    <h1 class="text-amber-500 font-semibold text-lg tracking-wide">{{ sb.title or "Untitled" }}</h1>
    <div class="text-neutral-500 text-sm flex gap-3 items-center">
      <span>{{ sb.shots|length }} beats</span>•
      <span><span id="paidCount" class="text-amber-500 font-medium">{{ paid }}</span> Tier-C</span>•
      <span>{{ sb.cultural_origin or "no culture set" }}</span>•
      <span class="{{ 'text-green-500' if sb.script_locked else 'text-yellow-500' }}">script {{ "locked" if sb.script_locked else "draft" }}</span>
    </div>
    <div class="flex-1"></div>
    <span class="text-sm font-medium {{ 'text-green-500' if sb.storyboard_approved else 'text-neutral-500' }}">{{ "APPROVED ✓" if sb.storyboard_approved else "not approved" }}</span>
    <button class="bg-amber-500 hover:bg-amber-400 text-neutral-950 font-bold py-2 px-5 rounded-lg shadow-lg shadow-amber-500/20 transition-all border border-amber-400" onclick="approve()">
      Approve storyboard →
    </button>
  </header>

  <div class="flex flex-1 items-start">
    <aside class="w-72 shrink-0 border-r border-neutral-800 min-h-[calc(100vh-65px)] p-4 sticky top-[65px] bg-neutral-900/50 overflow-y-auto">
      <h2 class="text-amber-500/70 text-xs font-bold uppercase tracking-widest mb-4">Projects</h2>
      <div class="flex flex-col gap-2">
      {% for p in projects %}
        <div class="block p-3 rounded-xl border {{ 'border-amber-500/50 bg-amber-500/10' if p.active else 'border-transparent hover:border-neutral-700 bg-neutral-900/50' }} cursor-pointer transition-colors group" onclick="selectProject('{{ p.rel }}')">
          <div class="text-sm font-medium text-neutral-200 group-hover:text-amber-400 truncate">{{ p.name }}</div>
          <div class="text-xs text-neutral-500 truncate mt-1">{{ p.rel }}</div>
        </div>
      {% else %}
        <div class="text-neutral-500 text-sm italic">No manifests found.</div>
      {% endfor %}
      </div>
    </aside>

    <main class="flex-1 max-w-7xl mx-auto p-6 lg:p-8">

      <!-- Configuration Panel -->
      <div class="bg-neutral-900 border border-neutral-800 rounded-2xl p-6 mb-8 shadow-sm">
        <h3 class="text-amber-500 font-medium uppercase tracking-wider text-xs mb-5">Generation Knobs (This Project)</h3>
        
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-4 items-end">
          <div class="md:col-span-2">
            <label class="block text-xs text-neutral-400 mb-1">Default Image Model (fal.ai)</label>
            <select id="k_backend" class="w-full bg-neutral-950 text-neutral-200 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 focus:outline-none">
              {% for v,label in backends.items() %}
              <option value="{{ v }}" {{ 'selected' if render.backend==v else '' }}>{{ label }}</option>
              {% endfor %}
            </select>
          </div>
          <div><label class="block text-xs text-neutral-400 mb-1">guidance_scale</label><input type="number" step="0.1" id="k_guidance" value="{{ render.guidance_scale }}" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none"></div>
          <div><label class="block text-xs text-neutral-400 mb-1">steps</label><input type="number" step="1" id="k_steps" value="{{ render.num_inference_steps }}" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none"></div>
        </div>

        <div class="mb-5">
          <label class="block text-xs text-neutral-400 mb-1">negative_prompt override (blank = built-in default)</label>
          <textarea id="k_negative" placeholder="{{ default_negative }}" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-3 text-sm focus:border-amber-500 outline-none min-h-[60px]">{{ render.negative_prompt }}</textarea>
        </div>

        <div class="flex items-center gap-4 pt-2 border-t border-neutral-800/50">
          <button onclick="saveRender()" class="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 border border-neutral-700 rounded-lg px-4 py-2 text-sm transition-colors">Save Knobs</button>
          
          <div class="flex items-center gap-3 ml-auto">
            <span class="text-xs text-neutral-500">Global Reference:</span>
            {% if render.reference_image %}
              <img src="/{{ render.reference_image }}" class="h-8 rounded border border-neutral-700">
              <button onclick="clearFrame()" class="text-xs text-red-400 hover:text-red-300">✕ remove</button>
            {% else %}
              <span class="text-xs text-neutral-600 italic">None</span>
            {% endif %}
            <div id="framedrop" class="border border-dashed border-neutral-700 hover:border-amber-500 rounded-lg px-4 py-2 text-xs text-neutral-400 cursor-pointer transition-colors"
                 ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                 ondragleave="this.classList.remove('border-amber-500')"
                 ondrop="dropFrame(event)"
                 onclick="document.getElementById('framefile').click()">⬆ Set Frame Edge</div>
            <input type="file" id="framefile" accept="image/*" class="hidden" onchange="uploadFrame(this.files[0])">
          </div>
        </div>
      </div>

      <!-- Vesper Chat Panel -->
      <div class="bg-neutral-900 border border-neutral-800 rounded-2xl p-6 mb-8 shadow-sm">
        <h3 class="text-amber-500 font-medium uppercase tracking-wider text-xs mb-4">Develop with Vesper (Claude) & Script Gate</h3>
        <div id="chatlog" class="max-h-64 overflow-y-auto bg-neutral-950 border border-neutral-800 rounded-xl p-4 mb-4 flex flex-col gap-3"></div>
        
        <div class="flex gap-3 mb-4 border-b border-neutral-800/50 pb-4">
          <input type="text" id="chatinput" placeholder="Ask Vesper to develop the entity / angle…" class="flex-1 bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none" onkeydown="if(event.key==='Enter')chatSend()">
          <button onclick="chatSend()" id="chatbtn" class="bg-neutral-800 hover:bg-neutral-700 border border-neutral-700 text-neutral-200 px-5 py-2 rounded-lg text-sm transition-colors">Send</button>
          <button onclick="scriptFromChat()" class="bg-neutral-800 hover:border-amber-500 border border-neutral-700 text-amber-500 px-5 py-2 rounded-lg text-sm transition-colors">Use Chat → Script</button>
        </div>

        <div class="flex gap-3 items-center">
          <input type="text" id="gen_topic" placeholder="Entity / topic to draft a full storyboard…" class="flex-1 bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none">
          <input type="number" id="gen_beats" placeholder="beats" min="1" class="w-24 bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none">
          <button onclick="genStoryboard()" id="draftbtn" class="bg-neutral-800 hover:border-amber-500 border border-neutral-700 text-neutral-200 px-5 py-2 rounded-lg text-sm transition-colors">Draft Storyboard</button>
          <button onclick="lockScript()" class="bg-green-900/40 text-green-500 border border-green-800 hover:bg-green-900/60 px-5 py-2 rounded-lg text-sm transition-colors">🔒 Lock Script</button>
        </div>
      </div>

      <!-- Render Pipeline Panel -->
      {% if sb.storyboard_approved %}
      <div class="bg-neutral-900 border border-green-900/50 rounded-2xl p-6 mb-8 shadow-lg shadow-green-900/10">
        <h3 class="text-green-500 font-medium uppercase tracking-wider text-xs mb-4">Pipeline Assembly (Approved ✓)</h3>
        
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
          <div class="flex flex-col gap-2">
            <button id="btn-narration" onclick="assemble('narration',this)" class="bg-neutral-800 border border-neutral-700 hover:border-amber-500 rounded-lg py-3 px-4 text-sm font-medium transition-colors">1. Generate Narration</button>
            <span id="st-narration" class="text-xs text-center text-neutral-500"></span>
          </div>
          <div class="flex flex-col gap-2">
            <button id="btn-render" onclick="assemble('render',this)" class="bg-neutral-800 border border-neutral-700 hover:border-amber-500 rounded-lg py-3 px-4 text-sm font-medium transition-colors">2. Render Clips (fal.ai)</button>
            <span id="st-render" class="text-xs text-center text-neutral-500"></span>
          </div>
          <div class="flex flex-col gap-2">
            <button id="btn-preview" onclick="assemble('preview',this)" class="bg-neutral-800 border border-neutral-700 hover:border-amber-500 rounded-lg py-3 px-4 text-sm font-medium transition-colors">3. Build Preview</button>
            <span id="st-preview" class="text-xs text-center text-neutral-500"></span>
          </div>
          <div class="flex flex-col gap-2">
            <button id="btn-timeline" onclick="assemble('timeline',this)" class="bg-neutral-800 border border-neutral-700 hover:border-amber-500 rounded-lg py-3 px-4 text-sm font-medium transition-colors">4. Export FCPXML</button>
            <span id="st-timeline" class="text-xs text-center text-neutral-500"></span>
          </div>
        </div>
        
        <div class="text-xs text-neutral-400 space-y-1 mb-4">
          {% if paid %}<div>• <span class="text-amber-500">{{ paid }} Tier-C shots</span> will route to fal.ai video endpoints.</div>{% endif %}
          {% if heroes %}<div>• <span class="text-amber-500">{{ heroes }} shots flagged Hero</span> require manual upload.</div>{% endif %}
        </div>

        {% if preview_url %}
        <div class="mt-4 rounded-xl overflow-hidden border border-neutral-800 bg-black">
          <video controls playsinline class="w-full max-h-[500px]" src="{{ preview_url }}?v={{ range(100000)|random }}"></video>
        </div>
        {% endif %}
        
        {% if fcpxml_ready %}
        <div class="mt-4 p-4 bg-amber-500/10 border border-amber-500/30 rounded-lg text-sm text-amber-500">
          ▶ Next step: Open <b>{{ ep_slug }}.fcpxml</b> in DaVinci Resolve (File → Import → Timeline) for final grading.
        </div>
        {% endif %}
      </div>
      {% endif %}

      <!-- Shot Grid -->
      <div class="space-y-6">
      {% for s in sb.shots %}
        <div class="bg-neutral-900 border rounded-2xl p-6 transition-all {{ 'border-green-800/50' if s.approved else 'border-neutral-800' }} {{ 'shadow-[inset_4px_0_0_#e0a458]' if s.flow_hero else '' }} {{ 'border-amber-500/30 bg-amber-500/5' if s.motion_type.value=='ai_video' else '' }}" id="beat-{{ s.scene_id }}">
          
          <div class="flex flex-col lg:flex-row gap-6 mb-6">
            <!-- Beat Info -->
            <div class="w-24 shrink-0">
              <div class="font-mono text-amber-500 text-sm font-bold">{{ s.scene_id }}</div>
              <div class="text-neutral-400 text-xs mt-1">⏱ {{ '%.1f'|format(s.camera.duration) }}s</div>
            </div>
            
            <!-- Text Prompts -->
            <div class="flex-1 space-y-4">
              <div>
                <label class="block text-xs text-neutral-400 mb-1">Narration ({{ '%.1f'|format(s.camera.duration) }}s slot)</label>
                <textarea onchange="saveField('{{ s.scene_id }}','narration',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-3 text-sm focus:border-amber-500 outline-none">{{ s.narration }}</textarea>
              </div>
              <div>
                <label class="block text-xs text-neutral-400 mb-1">Visual Prompt</label>
                <textarea onchange="saveField('{{ s.scene_id }}','prompt',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-3 text-sm focus:border-amber-500 outline-none">{{ s.prompt }}</textarea>
              </div>
              <div class="flex gap-4">
                <div class="flex-1">
                  <label class="block text-xs text-neutral-400 mb-1">Style/Medium</label>
                  <input type="text" value="{{ s.style_medium }}" onchange="saveField('{{ s.scene_id }}','style_medium',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none">
                </div>
              </div>

              {% if s.motion_type.value=='ai_video' or s.flow_hero %}
              <div class="mt-4 p-4 bg-black/30 rounded-xl border border-neutral-800">
                <label class="block text-xs text-amber-500/80 mb-2">🎬 Video Generation Prompt (Target ~{{ '%.0f'|format(s.camera.duration) }}s)</label>
                <textarea id="mp-{{ s.scene_id }}" onchange="saveField('{{ s.scene_id }}','motion_prompt',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-3 text-sm focus:border-amber-500 outline-none min-h-[80px]">{{ s.motion_prompt or motion_suggest[s.scene_id] }}</textarea>
                
                <div class="flex items-center gap-3 mt-3">
                  <button onclick="copyText('mp-{{ s.scene_id }}')" class="bg-neutral-800 border border-neutral-700 text-xs px-3 py-1.5 rounded-md hover:border-amber-500 transition-colors">Copy Prompt</button>
                  <div id="clipdrop-{{ s.scene_id }}" class="border border-dashed border-neutral-700 hover:border-amber-500 rounded-md px-4 py-1.5 text-xs text-neutral-400 cursor-pointer transition-colors"
                       ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                       ondragleave="this.classList.remove('border-amber-500')"
                       ondrop="dropClip(event,'{{ s.scene_id }}')"
                       onclick="document.getElementById('clipfile-{{ s.scene_id }}').click()">⬆ Manual Hero Import</div>
                  <input type="file" id="clipfile-{{ s.scene_id }}" accept="video/*" class="hidden" onchange="uploadClip('{{ s.scene_id }}',this.files[0])">
                  <span class="text-xs text-green-500 font-medium ml-auto">{% if s.hero_clip %}✓ Hero Clip Saved{% endif %}</span>
                </div>
              </div>
              {% endif %}
            </div>

            <!-- Controls -->
            <div class="w-48 shrink-0 flex flex-col gap-3">
              <label class="block text-xs text-neutral-400">Motion Tier</label>
              <select onchange="saveField('{{ s.scene_id }}','motion_type',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-sm focus:border-amber-500 outline-none {{ 'text-amber-500 border-amber-500/50' if s.motion_type.value=='ai_video' else '' }}">
                {% for v,label in tiers.items() %}
                <option value="{{ v }}" {{ 'selected' if s.motion_type.value==v else '' }}>{{ label }}</option>
                {% endfor %}
              </select>
              
              {% if s.motion_type.value=='ai_video' %}
              <label class="block text-xs text-neutral-400 mt-2">fal.ai Video Model</label>
              <select onchange="saveField('{{ s.scene_id }}','video_model',this.value)" class="w-full bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 text-xs focus:border-amber-500 outline-none text-amber-500">
                {% for v,label in video_backends.items() %}
                <option value="{{ v }}" {{ 'selected' if s.video_model==v else '' }}>{{ label }}</option>
                {% endfor %}
              </select>
              {% endif %}

              <label class="flex items-center gap-2 text-sm text-neutral-300 mt-2 cursor-pointer group">
                <input type="checkbox" {{ 'checked' if s.flow_hero else '' }} class="rounded border-neutral-700 text-amber-500 focus:ring-amber-500 bg-neutral-950" onchange="saveField('{{ s.scene_id }}','flow_hero',this.checked)">
                <span class="group-hover:text-amber-400 transition-colors">VEO/Flow Hero</span>
              </label>
            </div>
          </div>

          <!-- Assets Gallery -->
          <div class="border-t border-neutral-800/50 pt-5 mt-5">
            {% if s.draft_variations %}
            <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 mb-5">
              {% for path in s.draft_variations %}
              <div class="relative rounded-xl overflow-hidden cursor-pointer bg-black aspect-video group transition-all border-2 {{ 'border-amber-500' if s.chosen_variation==loop.index0 else 'border-transparent hover:border-neutral-600' }}" onclick="pick('{{ s.scene_id }}',{{ loop.index0 }},this)">
                <img src="/{{ path }}" loading="lazy" class="w-full h-full object-cover">
                <div class="absolute top-2 right-2 bg-amber-500 text-neutral-950 rounded-full w-6 h-6 flex items-center justify-center font-bold text-xs transition-all {{ 'opacity-100 scale-100' if s.chosen_variation==loop.index0 else 'opacity-0 scale-90' }}">✓</div>
              </div>
              {% endfor %}
            </div>
            {% else %}
            <div class="text-neutral-500 italic text-sm mb-5">No draft images generated yet.</div>
            {% endif %}

            <!-- Final Renders & Uploaders -->
            {% if shot_clips[s.scene_id] %}
            <div class="mb-5 rounded-xl overflow-hidden border border-neutral-800 bg-black max-w-lg">
              <video controls playsinline preload="none" {% if s.draft_image %}poster="/{{ s.draft_image }}"{% endif %} class="w-full aspect-video object-cover" src="{{ shot_clips[s.scene_id] }}?v={{ range(100000)|random }}"></video>
              <div class="bg-neutral-900 px-3 py-2 text-xs text-neutral-400">▶ {% if s.hero_clip %}Imported Hero Clip{% else %}Rendered {{ s.motion_type.value }}{% endif %}</div>
            </div>
            {% endif %}

            <div class="flex flex-wrap gap-4 items-center justify-between">
              <div class="flex items-center gap-2">
                <select id="be-{{ s.scene_id }}" class="bg-neutral-950 border border-neutral-800 rounded-lg p-2 text-xs focus:border-amber-500 outline-none text-neutral-300">
                  {% for v,label in backends.items() %}
                  <option value="{{ v }}" {{ 'selected' if render.backend==v else '' }}>{{ label }}</option>
                  {% endfor %}
                </select>
                <button onclick="regen('{{ s.scene_id }}',this)" class="bg-neutral-800 hover:bg-neutral-700 border border-neutral-700 text-neutral-200 text-xs px-4 py-2 rounded-lg transition-colors flex items-center">↻ Generate Drafts</button>
                <div id="imgdrop-{{ s.scene_id }}" class="border border-dashed border-neutral-700 hover:border-amber-500 rounded-lg px-4 py-2 text-xs text-neutral-400 cursor-pointer transition-colors ml-2"
                     ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                     ondragleave="this.classList.remove('border-amber-500')"
                     ondrop="dropImage(event,'{{ s.scene_id }}')"
                     onclick="document.getElementById('imgfile-{{ s.scene_id }}').click()">⬆ Manual Image Upload</div>
                <input type="file" id="imgfile-{{ s.scene_id }}" accept="image/*" class="hidden" onchange="uploadImage('{{ s.scene_id }}',this.files[0])">
              </div>

              <!-- References -->
              <div class="flex flex-wrap gap-2 items-center">
                {% for r in shot_refs[s.scene_id] %}
                  <div class="relative w-16 h-10 border border-neutral-700 rounded overflow-hidden bg-black group" title="{{ r.name }}">
                    {% if r.file %}<img src="/references/{{ r.file }}" class="w-full h-full object-cover">{% else %}<div class="text-[10px] text-neutral-500 p-1">{{ r.name }}</div>{% endif %}
                    <span class="absolute top-0 right-0 w-4 h-4 leading-4 text-center text-[10px] cursor-pointer bg-black/70 text-amber-500 rounded-bl opacity-0 group-hover:opacity-100 hover:bg-red-900 hover:text-white transition-all" onclick="removeRef('{{ s.scene_id }}','{{ r.name }}')">✕</span>
                  </div>
                {% endfor %}
                <div id="drop-{{ s.scene_id }}" class="border border-dashed border-neutral-700 hover:border-amber-500 rounded px-3 h-10 flex items-center text-[11px] text-neutral-500 cursor-pointer transition-colors"
                     ondragover="event.preventDefault();this.classList.add('border-amber-500')"
                     ondragleave="this.classList.remove('border-amber-500')"
                     ondrop="dropRef(event,'{{ s.scene_id }}')"
                     onclick="document.getElementById('file-{{ s.scene_id }}').click()">+ reference</div>
                <input type="file" id="file-{{ s.scene_id }}" accept="image/*" class="hidden" onchange="uploadRef('{{ s.scene_id }}',this.files[0])">
              </div>
            </div>
          </div>
          
        </div>
      {% endfor %}
      </div>
    </main>
  </div>

  <div id="toast" class="fixed bottom-6 left-1/2 -translate-x-1/2 bg-black border border-amber-500 text-amber-500 px-5 py-2.5 rounded-xl shadow-lg opacity-0 transition-opacity pointer-events-none z-50 text-sm font-medium"></div>

<script>
function btnSpin(btn, text) {
    btn.disabled = true;
    btn.innerHTML = `<svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-current inline-block" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> ${text}`;
}
function btnReset(btn, text) { btn.disabled = false; btn.innerHTML = text; }

function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('toast-enter');
  setTimeout(()=>t.classList.remove('toast-enter'),2000); }
async function post(url,body){ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})}); return {ok:r.ok, data:await r.json()}; }

function copyText(id){ const el=document.getElementById(id); if(!el) return;
  navigator.clipboard.writeText(el.value).then(()=>toast('Prompt copied')).catch(()=>{ el.select(); document.execCommand('copy'); toast('Prompt copied'); }); }

async function saveField(sid,field,val){ const b={}; b[field]=val; const {data}=await post('/api/shot/'+sid,b);
  if(field==='motion_type'){ document.getElementById('paidCount').textContent=data.paid_count;
    const bt=document.getElementById('beat-'+sid); 
    if(val==='ai_video'){ bt.classList.add('border-amber-500/30','bg-amber-500/5'); } else { bt.classList.remove('border-amber-500/30','bg-amber-500/5'); }
  }
  if(field==='flow_hero'){ const bt=document.getElementById('beat-'+sid); if(val) bt.classList.add('shadow-[inset_4px_0_0_#e0a458]'); else bt.classList.remove('shadow-[inset_4px_0_0_#e0a458]'); }
  toast('Saved'); }

async function saveRender(){ const body={ backend:document.getElementById('k_backend').value,
  guidance_scale:parseFloat(document.getElementById('k_guidance').value),
  nag_scale:parseFloat(document.getElementById('k_nag').value),
  num_inference_steps:parseInt(document.getElementById('k_steps').value),
  negative_prompt:document.getElementById('k_negative').value };
  const {ok}=await post('/api/render',body); toast(ok?'Knobs saved':'Save failed'); }

async function pick(sid,idx,el){ 
  const container = el.parentNode;
  container.querySelectorAll('div').forEach(v=>{ v.classList.remove('border-amber-500'); v.classList.add('border-transparent'); });
  container.querySelectorAll('.absolute').forEach(v=>{ v.classList.remove('opacity-100','scale-100'); v.classList.add('opacity-0','scale-90'); });
  el.classList.remove('border-transparent'); el.classList.add('border-amber-500');
  el.querySelector('.absolute').classList.remove('opacity-0','scale-90'); el.querySelector('.absolute').classList.add('opacity-100','scale-100');
  await post('/api/shot/'+sid,{chosen_variation:idx}); toast('Selected variation '+(idx+1)); }

async function regen(sid,btn){
  const be=document.getElementById('be-'+sid).value;
  const cost = be==='flux-pro' ? 'FLUX.2 [pro] (~$0.045/img)' 
             : be==='flux-ultra' ? 'FLUX 1.1 [pro] Ultra (~$0.06/img)'
             : be==='flux-turbo' ? 'FLUX.1 [dev] Turbo (~$0.008/img)'
             : 'Nano Banana (~$0.15/img)';
  if(!confirm('\\u26A0 PAID API CALL: Generate 3 variations using '+cost+'? This counts against the local server session limit.')) return;
  btnSpin(btn, 'Generating...');
  const {ok,data}=await post('/api/regenerate/'+sid,{backend:be}); 
  btnReset(btn, '↻ Generate Drafts');
  if(ok){ toast('Generated ('+data.regen_used+'/'+data.regen_limit+' used)'); setTimeout(()=>location.reload(),500);}
  else { toast((data&&data.error)?data.error:'Generation failed'); } }

async function approve(){ const {ok,data}=await post('/api/approve');
  if(ok){ toast(data.gate_cleared?'Approved — Pipeline Unlocked':'Approved'); setTimeout(()=>location.reload(),700); }
  else { alert('Cannot approve yet:\\n'+(data.error||'')+'\\n'+(data.scenes||[]).join(', ')); } }

async function selectProject(rel){ const {ok}=await post('/api/project/select',{rel:rel});
  if(ok){ location.reload(); } else { toast('Could not open project'); } }

function addFile(sid,file){ const fd=new FormData(); fd.append('file',file);
  return fetch('/api/shot/'+sid+'/reference',{method:'POST',body:fd}).then(r=>r.json()); }
async function uploadRef(sid,file){ if(!file) return; const d=await addFile(sid,file);
  if(d.ok){ toast('Reference added'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'Upload failed'); } }
function dropRef(ev,sid){ ev.preventDefault(); document.getElementById('drop-'+sid).classList.remove('border-amber-500');
  const f=ev.dataTransfer.files[0]; if(f) uploadRef(sid,f); }
async function removeRef(sid,name){ const {ok}=await post('/api/shot/'+sid+'/reference/remove',{name:name});
  if(ok){ toast('Reference removed'); setTimeout(()=>location.reload(),300);} else { toast('Remove failed'); } }

async function uploadImage(sid,file){ if(!file) return; toast('Uploading image...');
  const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/shot/'+sid+'/image',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('Uploaded & selected'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'Upload failed'); } }
function dropImage(ev,sid){ ev.preventDefault(); document.getElementById('imgdrop-'+sid).classList.remove('border-amber-500');
  const f=ev.dataTransfer.files[0]; if(f) uploadImage(sid,f); }

async function uploadClip(sid,file){ if(!file) return; toast('Importing & normalizing clip...'); const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/shot/'+sid+'/clip',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('Hero clip imported ('+d.duration+'s)'); setTimeout(()=>location.reload(),500);} else { toast(d.error||'Import failed'); } }
function dropClip(ev,sid){ ev.preventDefault(); document.getElementById('clipdrop-'+sid).classList.remove('border-amber-500');
  const f=ev.dataTransfer.files[0]; if(f) uploadClip(sid,f); }

async function uploadFrame(file){ if(!file) return; toast('Uploading frame...'); const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/render/reference',{method:'POST',body:fd}); const d=await r.json();
  if(d.ok){ toast('Frame reference set'); setTimeout(()=>location.reload(),400);} else { toast(d.error||'Upload failed'); } }
function dropFrame(ev){ ev.preventDefault(); document.getElementById('framedrop').classList.remove('border-amber-500');
  const f=ev.dataTransfer.files[0]; if(f) uploadFrame(f); }
async function clearFrame(){ const {ok}=await post('/api/render/reference/clear'); if(ok){ toast('Frame cleared'); setTimeout(()=>location.reload(),300);} }

let chat=[];
function logMsg(role,text){ const l=document.getElementById('chatlog');
  const d=document.createElement('div'); d.className='text-sm mb-2 '+(role==='user'?'text-neutral-300':'text-amber-500 whitespace-pre-wrap');
  d.textContent=(role==='user'?'You: ':'Vesper: ')+text; l.appendChild(d); l.scrollTop=l.scrollHeight; }
async function chatSend(){ const inp=document.getElementById('chatinput'); const text=inp.value.trim(); if(!text) return;
  const btn = document.getElementById('chatbtn'); btnSpin(btn, 'Thinking');
  inp.value=''; logMsg('user',text); chat.push({role:'user',content:text});
  const {ok,data}=await post('/chat/develop',{messages:chat});
  btnReset(btn, 'Send');
  if(ok){ logMsg('assistant',data.reply); chat.push({role:'assistant',content:data.reply}); }
  else { logMsg('assistant','[error] '+(data.error||'failed')); } }

async function scriptFromChat(){
  if(!chat.length){ toast('Chat with Vesper first'); return; }
  if(!confirm('\\u26A0 DESTRUCTIVE: OVERWRITE the active project with this conversation? (All chosen drafts will be reset).')) return;
  const beats=document.getElementById('gen_beats').value;
  toast('Writing script...');
  const {ok,data}=await post('/api/script/from_chat',{messages:chat,beats:beats||null});
  if(ok){ toast('Scripted '+data.shots+' beats'); setTimeout(()=>location.reload(),600); }
  else { alert('Failed:\\n'+(data.error||'')); } }

async function genStoryboard(){ const topic=document.getElementById('gen_topic').value.trim(); if(!topic){ toast('Enter a topic'); return; }
  if(!confirm('\\u26A0 DESTRUCTIVE: A fresh AI draft will OVERWRITE the active project completely. Continue?')) return;
  const beats=document.getElementById('gen_beats').value;
  const btn=document.getElementById('draftbtn'); btnSpin(btn, 'Drafting');
  const {ok,data}=await post('/api/script/generate',{topic:topic,beats:beats||null});
  if(ok){ toast('Drafted '+data.shots+' beats'); setTimeout(()=>location.reload(),600); } else { btnReset(btn, 'Draft Storyboard'); alert('Draft failed:\\n'+(data.error||'')); } }

async function lockScript(){ const {ok,data}=await post('/api/script/lock');
  if(ok){ toast('Script locked'); setTimeout(()=>location.reload(),500); } else { alert('Cannot lock:\\n'+(data.error||'')); } }

async function assemble(stage,btn){ btnSpin(btn, 'Starting...');
  const {ok,data}=await post('/api/assemble/'+stage,{});
  if(!ok){ toast(data.error||'Could not start'); btnReset(btn, btn.textContent.replace('Starting...','')); return; }
  toast(stage+' running'); pollAssemble(); }

let _lastStatus={};
async function pollAssemble(){ let r; try{ r=await fetch('/api/assemble/status'); }catch(e){ return; }
  const d=await r.json(); let running=false, justFinished=false;
  for(const [k,v] of Object.entries(d.jobs||{})){
    const el=document.getElementById('st-'+k), b=document.getElementById('btn-'+k);
    if(el){ el.textContent=v.status+(v.status==='error'?' \\u2014 Check terminal logs':'');
      el.className = 'text-xs text-center font-medium ' + (v.status==='error'?'text-red-500':(v.status==='done'?'text-green-500':'text-amber-500 animate-pulse')); }
    if(b && v.status==='running'){ btnSpin(b, 'Processing...'); }
    if(v.status==='running') running=true;
    if(_lastStatus[k]==='running' && v.status!=='running') justFinished=true;
    _lastStatus[k]=v.status; }
  if(running) setTimeout(pollAssemble,2500);
  else if(justFinished) setTimeout(()=>location.reload(),800); } 
if(document.getElementById('btn-narration')) pollAssemble();
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
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
        fcpxml_ready=fcpxml_ready, ep_slug=ep["slug"],
        motion_suggest={s.scene_id: _suggest_motion_prompt(s) for s in sb.shots},
    )


# --------------------------------------------------------------------------- #
# GCS Absolute Pathing Fix (replaces send_from_directory)
# --------------------------------------------------------------------------- #
@app.get("/assets/<scene>/<path:filename>")
def asset(scene: str, filename: str):
    # Absolute pathing completely bypasses the Flask subpath error while maintaining security bounds
    target_dir = os.path.abspath(str(config.ASSETS / scene))
    abs_path = os.path.abspath(os.path.join(target_dir, filename))
    if not abs_path.startswith(target_dir):
        abort(403)
    return send_file(abs_path) if os.path.exists(abs_path) else abort(404)


@app.get("/references/<path:filename>")
def reference_file(filename: str):
    target_dir = os.path.abspath(str(config.REFERENCES_DIR))
    abs_path = os.path.abspath(os.path.join(target_dir, filename))
    if not abs_path.startswith(target_dir):
        abort(403)
    return send_file(abs_path) if os.path.exists(abs_path) else abort(404)


@app.get("/render/<path:filename>")
def render_file(filename: str):
    target_dir = os.path.abspath(str(config.RENDER_DIR))
    abs_path = os.path.abspath(os.path.join(target_dir, filename))
    if not abs_path.startswith(target_dir):
        abort(403)
    return send_file(abs_path) if os.path.exists(abs_path) else abort(404)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/project/select")
def select_project():
    data = request.get_json(force=True) or {}
    rel = (data.get("rel") or "").strip()
    target = (WORKSPACE_ROOT / rel).resolve()
    root = WORKSPACE_ROOT.resolve()
    if (target.name != "storyboard_manifest.json" or not target.exists()
            or root not in target.parents):
        return jsonify(ok=False, error="not a valid project manifest"), 400
    _state["manifest"] = target
    return jsonify(ok=True, active=str(target.relative_to(root)).replace("\\", "/"))


@app.post("/api/render")
def update_render():
    sb = _load()
    data = request.get_json(force=True) or {}
    r = sb.render
    if "backend" in data and str(data["backend"]) in ALLOWED_BACKENDS:
        r.backend = str(data["backend"])
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
    sb.render.reference_image = str(dest.relative_to(config.ROOT)).replace("\\", "/")
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
    if "motion_type" in data:
        shot.motion_type = MotionType(data["motion_type"])
        # Tier C needs a video model for the gate; other tiers clear it.
        if shot.motion_type == MotionType.AI_VIDEO:
            shot.video_model = shot.video_model or DEFAULT_VIDEO_MODEL
        else:
            shot.video_model = None
    if "video_model" in data:
        shot.video_model = data["video_model"]
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
    """Upload a finished image made outside the pipeline as a draft for this shot."""
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

    rel = str(dest.relative_to(config.ROOT)).replace("\\", "/")
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
    """Import a finished hero video (Veo/Flow) as this shot's render clip."""
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
                   path=str(dest.relative_to(config.ROOT)).replace("\\", "/"))


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
    if backend not in ALLOWED_BACKENDS:
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


@app.post("/api/script/generate")
def script_generate():
    """Draft a full structured storyboard from a topic via src/script.py (Claude)."""
    from . import script

    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify(ok=False, error="topic is required"), 400
    beats = data.get("beats")
    try:
        sb = script.generate_script(topic, num_beats=int(beats) if beats else None)
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
    try:
        sb = script.generate_script_from_messages(messages, num_beats=int(beats) if beats else None)
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

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=script.DEFAULT_MODEL, max_tokens=2000,
            system=script.SYSTEM_PROMPT, messages=messages,
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
        from . import motion
        fn = lambda: motion.render_all(storyboard=sb, placeholders=True)  # noqa: E731
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


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    print(f"Studio workspace: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run(debug=True)
