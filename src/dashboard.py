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
BACKENDS = {"nano2": "Nano Banana 2 (~$0.15)", "flux-cfg": "flux-general (~$0.04)"}
ALLOWED_BACKENDS = {"nano2", "flux-cfg", "nano", "flux", "flux-lora"}

# Root under which we look for sibling projects, and dirs we never descend into.
WORKSPACE_ROOT = config.ROOT
IGNORE_DIRS = {".venv", ".git", "__pycache__", "assets", "audio", "audio_pool",
               "lora_training", "render", "models", "sizzle", "intro", "references",
               "node_modules", "scripts", "src", "tmp", "temp", "output", "cache"}

# Active project manifest (this is a single-user local tool, so module state is fine).
_state = {"manifest": config.MANIFEST_PATH}

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
<!doctype html><html><head><meta charset="utf-8"><title>{{ sb.title or "Untitled" }} — Studio</title>
<style>
  :root { --bg:#14110e; --card:#1e1a15; --line:#3a3128; --amber:#e0a458; --ink:#d8cdbd; --dim:#8a7c68; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  a { color:var(--amber); text-decoration:none; }
  header { position:sticky; top:0; z-index:10; background:#100d0a; border-bottom:1px solid var(--line);
    padding:12px 20px; display:flex; align-items:center; gap:18px; }
  header h1 { font-size:17px; margin:0; color:var(--amber); font-weight:600; }
  .meta { color:var(--dim); font-size:13px; }
  .spacer { flex:1; }
  button { font:inherit; cursor:pointer; border:1px solid var(--line); background:var(--card);
    color:var(--ink); padding:7px 12px; border-radius:6px; }
  button:hover { border-color:var(--amber); }
  .approve { background:var(--amber); color:#14110e; border-color:var(--amber); font-weight:600; padding:8px 16px; }
  .wrap { display:flex; align-items:flex-start; }
  aside { width:240px; flex:none; border-right:1px solid var(--line); min-height:100vh; padding:14px; position:sticky; top:53px; }
  aside h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin:14px 0 8px; }
  .proj { display:block; padding:7px 9px; border-radius:6px; border:1px solid transparent; cursor:pointer;
    font-size:13px; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .proj:hover { border-color:var(--line); }
  .proj.active { border-color:var(--amber); color:var(--amber); background:#241f18; }
  .proj small { color:var(--dim); display:block; font-size:11px; }
  main { flex:1; max-width:1080px; margin:0 auto; padding:20px; }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; margin-bottom:18px; }
  .panel h3 { margin:0 0 10px; font-size:14px; color:var(--amber); }
  label { font-size:12px; color:var(--dim); display:block; margin-bottom:3px; }
  .knobs { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  input[type=number], input[type=text], textarea { width:100%; background:#14110e; color:var(--ink);
    border:1px solid var(--line); border-radius:6px; padding:7px; font:inherit; }
  textarea { resize:vertical; min-height:52px; }
  select { font:inherit; background:#14110e; color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:6px; }
  .tierC select { border-color:var(--amber); color:var(--amber); }
  .beat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .beat.approved { border-color:#4a7a3a; }
  .beat.hero { box-shadow:inset 3px 0 0 var(--amber); }
  .beat-top { display:flex; gap:14px; align-items:flex-start; margin-bottom:10px; }
  .sid { font-family:ui-monospace,monospace; color:var(--amber); font-size:13px; padding-top:6px; min-width:44px; }
  .ctrls { display:flex; flex-direction:column; gap:8px; align-items:flex-end; }
  .hero-tog { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--dim); cursor:pointer; }
  .vars { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:12px; }
  .var { position:relative; border:2px solid transparent; border-radius:8px; overflow:hidden; cursor:pointer;
    background:#0d0b08; aspect-ratio:16/9; }
  .var img { width:100%; height:100%; object-fit:cover; display:block; }
  .var.sel { border-color:var(--amber); }
  .var .tick { position:absolute; top:6px; right:6px; background:var(--amber); color:#14110e;
    border-radius:50%; width:22px; height:22px; text-align:center; line-height:22px; font-weight:700; display:none; }
  .var.sel .tick { display:block; }
  .empty { color:var(--dim); font-style:italic; padding:16px 0; }
  .row { display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  .refs { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
  .ref { position:relative; width:64px; height:40px; border:1px solid var(--line); border-radius:5px; overflow:hidden; background:#0d0b08; }
  .ref img { width:100%; height:100%; object-fit:cover; }
  .ref .tag { font-size:10px; color:var(--dim); padding:2px 4px; }
  .refx { position:absolute; top:0; right:0; width:16px; height:16px; line-height:15px; text-align:center;
    font-size:11px; cursor:pointer; background:rgba(0,0,0,.7); color:var(--amber); border-bottom-left-radius:5px; }
  .refx:hover { background:#a33; color:#fff; }
  .drop { border:1px dashed var(--line); border-radius:6px; padding:8px 12px; font-size:12px; color:var(--dim); cursor:pointer; }
  .drop.over { border-color:var(--amber); color:var(--amber); }
  .chatlog { max-height:220px; overflow-y:auto; border:1px solid var(--line); border-radius:6px; padding:8px; margin-bottom:8px; background:#14110e; }
  .msg { margin-bottom:8px; font-size:13px; } .msg.u { color:var(--ink); } .msg.a { color:var(--amber); white-space:pre-wrap; }
  #toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#000;
    border:1px solid var(--amber); color:var(--amber); padding:10px 18px; border-radius:8px;
    opacity:0; transition:.2s; pointer-events:none; } #toast.show { opacity:1; }
</style></head><body>
<header>
  <h1>{{ sb.title or "Untitled" }}</h1>
  <span class="meta">{{ sb.shots|length }} beats · <span id="paidCount">{{ paid }}</span> Tier-C
    · {{ sb.cultural_origin or "no culture set" }}
    · script {{ "locked" if sb.script_locked else "draft" }}</span>
  <span class="spacer"></span>
  <span class="meta">{{ "APPROVED ✓" if sb.storyboard_approved else "not approved" }}</span>
  <button class="approve" onclick="approve()">Approve storyboard →</button>
</header>
<div class="wrap">
<aside>
  <h2>Projects</h2>
  {% for p in projects %}
    <div class="proj {{ 'active' if p.active else '' }}" onclick="selectProject('{{ p.rel }}')">
      {{ p.name }}<small>{{ p.rel }}</small>
    </div>
  {% else %}
    <div class="meta">No manifests found.</div>
  {% endfor %}
</aside>
<main>

  <div class="panel">
    <h3>Generation knobs (this project)</h3>
    <div class="row" style="margin-top:0;margin-bottom:10px">
      <label style="margin:0">Default image model</label>
      <select id="k_backend">
        {% for v,label in backends.items() %}
        <option value="{{ v }}" {{ 'selected' if render.backend==v else '' }}>{{ label }}</option>
        {% endfor %}
      </select>
      <span class="meta">nag/steps below apply to flux-general only</span>
    </div>
    <div class="knobs">
      <div><label>guidance_scale</label><input type="number" step="0.1" id="k_guidance" value="{{ render.guidance_scale }}"></div>
      <div><label>nag_scale (neg strength)</label><input type="number" step="0.1" id="k_nag" value="{{ render.nag_scale }}"></div>
      <div><label>num_inference_steps</label><input type="number" step="1" id="k_steps" value="{{ render.num_inference_steps }}"></div>
    </div>
    <div style="margin-top:10px"><label>negative_prompt override (blank = built-in default)</label>
      <textarea id="k_negative" placeholder="{{ default_negative }}">{{ render.negative_prompt }}</textarea></div>
    <div class="row" style="margin-top:10px">
      <label style="margin:0">Global frame reference</label>
      {% if render.reference_image %}
        <img src="/{{ render.reference_image }}" style="height:40px;border:1px solid var(--line);border-radius:4px">
        <button onclick="clearFrame()">✕ remove</button>
      {% else %}
        <span class="meta">none — shots may drift to different borders</span>
      {% endif %}
      <div class="drop" id="framedrop"
           ondragover="event.preventDefault();this.classList.add('over')"
           ondragleave="this.classList.remove('over')"
           ondrop="dropFrame(event)"
           onclick="document.getElementById('framefile').click()">⬆ set frame (border/page-edge)</div>
      <input type="file" id="framefile" accept="image/*" style="display:none" onchange="uploadFrame(this.files[0])">
      <span class="meta">Nano Banana 2 only</span>
    </div>
    <div class="row"><button onclick="saveRender()">Save knobs</button></div>
  </div>

  <div class="panel">
    <h3>Develop with Vesper (Claude) &amp; script gate</h3>
    <div class="chatlog" id="chatlog"></div>
    <div class="row" style="margin-top:0">
      <input type="text" id="chatinput" placeholder="Ask Vesper to develop the entity / angle…" style="flex:1"
        onkeydown="if(event.key==='Enter')chatSend()">
      <button onclick="chatSend()">Send</button>
      <button class="approve" onclick="scriptFromChat()">Use chat → script</button>
    </div>
    <div class="row">
      <input type="text" id="gen_topic" placeholder="Entity / topic to draft a full storyboard…" style="flex:1">
      <input type="number" id="gen_beats" placeholder="beats" style="width:80px" min="1">
      <button onclick="genStoryboard()">Draft storyboard</button>
      <button onclick="lockScript()">🔒 Lock script</button>
    </div>
  </div>

  {% if sb.storyboard_approved %}
  <div class="panel">
    <h3>Assemble — storyboard approved ✓</h3>
    <div class="row"><button id="btn-narration" onclick="assemble('narration',this)">1 · Generate narration</button>
      <span id="st-narration" class="meta"></span></div>
    <div class="row"><button id="btn-render" onclick="assemble('render',this)">2 · Render clips (local, slow)</button>
      <span id="st-render" class="meta"></span></div>
    <div class="row"><button id="btn-preview" onclick="assemble('preview',this)">3 · Build preview (watch the cut)</button>
      <span id="st-preview" class="meta"></span></div>
    <div class="row"><button id="btn-timeline" onclick="assemble('timeline',this)">4 · Build DaVinci timeline (OTIO + FCPXML)</button>
      <span id="st-timeline" class="meta"></span></div>
    {% if paid %}<div class="meta">{{ paid }} Tier-C (ai_video) shot(s) render as placeholders until the paid fal video stage (Seedance/Kling) is wired.</div>{% endif %}
    {% if heroes %}<div class="meta">{{ heroes }} shot(s) flagged VEO/Flow hero — hand-animate those in Flow and import the clips.</div>{% endif %}
    {% if preview_url %}
    <div style="margin-top:12px">
      <video controls playsinline style="width:100%;max-height:440px;background:#000;border:1px solid var(--line);border-radius:8px"
             src="{{ preview_url }}?v={{ range(100000)|random }}"></video>
      <div class="meta">Assembled preview — narration + music + all clips. This is the review proxy, not the master.</div>
    </div>
    {% endif %}
    {% if fcpxml_ready %}
    <div class="meta" style="margin-top:8px;color:var(--amber)">▶ Next step: open <b>{{ ep_slug }}.fcpxml</b> in DaVinci Resolve
      (File → Import → Timeline) to finish the master cut.</div>
    {% endif %}
    <div class="meta">Runs on the server; outputs are namespaced to this episode. Watch status here or the terminal.</div>
  </div>
  {% endif %}

{% for s in sb.shots %}
  <div class="beat {{ 'approved' if s.approved else '' }} {{ 'hero' if s.flow_hero else '' }} {{ 'tierC' if s.motion_type.value=='ai_video' else '' }}" id="beat-{{ s.scene_id }}">
    <div class="beat-top">
      <div class="sid">{{ s.scene_id }}
        <div style="color:var(--amber);font-size:12px;font-weight:400;margin-top:4px">⏱ {{ '%.1f'|format(s.camera.duration) }}s</div></div>
      <div style="flex:1">
        <label>narration · {{ '%.1f'|format(s.camera.duration) }}s slot</label>
        <textarea onchange="saveField('{{ s.scene_id }}','narration',this.value)">{{ s.narration }}</textarea>
        <label style="margin-top:8px">scene (visual)</label>
        <textarea onchange="saveField('{{ s.scene_id }}','prompt',this.value)">{{ s.prompt }}</textarea>
        <label style="margin-top:8px">style_medium</label>
        <input type="text" value="{{ s.style_medium }}" onchange="saveField('{{ s.scene_id }}','style_medium',this.value)">
        {% if s.motion_type.value=='ai_video' or s.flow_hero %}
        <label style="margin-top:8px">🎬 video-gen prompt (Veo/Flow/Seedance) · animate to ~{{ '%.0f'|format(s.camera.duration) }}s</label>
        <textarea id="mp-{{ s.scene_id }}" onchange="saveField('{{ s.scene_id }}','motion_prompt',this.value)">{{ s.motion_prompt or motion_suggest[s.scene_id] }}</textarea>
        <div class="row" style="margin-top:4px">
          <button onclick="copyText('mp-{{ s.scene_id }}')">Copy prompt</button>
          <div class="drop" id="clipdrop-{{ s.scene_id }}"
               ondragover="event.preventDefault();this.classList.add('over')"
               ondragleave="this.classList.remove('over')"
               ondrop="dropClip(event,'{{ s.scene_id }}')"
               onclick="document.getElementById('clipfile-{{ s.scene_id }}').click()">⬆ import hero clip (Veo/Flow)</div>
          <input type="file" id="clipfile-{{ s.scene_id }}" accept="video/*" style="display:none"
                 onchange="uploadClip('{{ s.scene_id }}',this.files[0])">
          <span class="meta">{% if s.hero_clip %}✓ hero clip imported{% else %}target ~{{ '%.0f'|format(s.camera.duration) }}s{% endif %}</span>
        </div>
        {% endif %}
      </div>
      <div class="ctrls">
        <select onchange="saveField('{{ s.scene_id }}','motion_type',this.value)">
          {% for v,label in tiers.items() %}
          <option value="{{ v }}" {{ 'selected' if s.motion_type.value==v else '' }}>{{ label }}</option>
          {% endfor %}
        </select>
        <label class="hero-tog">
          <input type="checkbox" {{ 'checked' if s.flow_hero else '' }}
            onchange="saveField('{{ s.scene_id }}','flow_hero',this.checked)"> VEO/Flow hero
        </label>
      </div>
    </div>

    {% if s.draft_variations %}
    <div class="vars">
      {% for path in s.draft_variations %}
      <div class="var {{ 'sel' if s.chosen_variation==loop.index0 else '' }}"
           onclick="pick('{{ s.scene_id }}',{{ loop.index0 }},this)">
        <img src="/{{ path }}" loading="lazy"><div class="tick">✓</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">No drafts yet.</div>
    {% endif %}

    {% if shot_clips[s.scene_id] %}
    <div style="margin-top:10px">
      <video controls playsinline preload="none"
             {% if s.draft_image %}poster="/{{ s.draft_image }}"{% endif %}
             style="width:100%;max-height:240px;background:#000;border-radius:8px"
             src="{{ shot_clips[s.scene_id] }}?v={{ range(100000)|random }}"></video>
      <div class="visual">▶ {% if s.hero_clip %}imported hero clip (Veo/Flow){% else %}rendered {{ s.motion_type.value }} clip{% endif %}</div>
    </div>
    {% endif %}

    <div class="refs">
      {% for r in shot_refs[s.scene_id] %}
        <div class="ref" title="{{ r.name }}">
          {% if r.file %}<img src="/references/{{ r.file }}">{% else %}<div class="tag">{{ r.name }}</div>{% endif %}
          <span class="refx" title="remove reference" onclick="removeRef('{{ s.scene_id }}','{{ r.name }}')">✕</span>
        </div>
      {% endfor %}
      <div class="drop" id="drop-{{ s.scene_id }}"
           ondragover="event.preventDefault();this.classList.add('over')"
           ondragleave="this.classList.remove('over')"
           ondrop="dropRef(event,'{{ s.scene_id }}')"
           onclick="document.getElementById('file-{{ s.scene_id }}').click()">+ drop / click to add reference</div>
      <input type="file" id="file-{{ s.scene_id }}" accept="image/*" style="display:none"
             onchange="uploadRef('{{ s.scene_id }}',this.files[0])">
    </div>

    <div class="row">
      <select id="be-{{ s.scene_id }}" title="image model for this regenerate">
        {% for v,label in backends.items() %}
        <option value="{{ v }}" {{ 'selected' if render.backend==v else '' }}>{{ label }}</option>
        {% endfor %}
      </select>
      <button onclick="regen('{{ s.scene_id }}',this)">↻ Regenerate</button>
      <div class="drop" id="imgdrop-{{ s.scene_id }}"
           ondragover="event.preventDefault();this.classList.add('over')"
           ondragleave="this.classList.remove('over')"
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
async function post(url,body){ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})}); return {ok:r.ok, data:await r.json()}; }

function copyText(id){ const el=document.getElementById(id); if(!el) return;
  navigator.clipboard.writeText(el.value).then(()=>toast('prompt copied')).catch(()=>{ el.select(); document.execCommand('copy'); toast('prompt copied'); }); }

async function saveField(sid,field,val){ const b={}; b[field]=val; const {data}=await post('/api/shot/'+sid,b);
  if(field==='motion_type'){ document.getElementById('paidCount').textContent=data.paid_count;
    document.getElementById('beat-'+sid).classList.toggle('tierC',val==='ai_video'); }
  if(field==='flow_hero'){ document.getElementById('beat-'+sid).classList.toggle('hero',val); }
  toast(sid+' saved'); }

async function saveRender(){ const body={ backend:document.getElementById('k_backend').value,
  guidance_scale:parseFloat(document.getElementById('k_guidance').value),
  nag_scale:parseFloat(document.getElementById('k_nag').value),
  num_inference_steps:parseInt(document.getElementById('k_steps').value),
  negative_prompt:document.getElementById('k_negative').value };
  const {ok}=await post('/api/render',body); toast(ok?'knobs saved':'save failed'); }

async function pick(sid,idx,el){ el.parentNode.querySelectorAll('.var').forEach(v=>v.classList.remove('sel'));
  el.classList.add('sel'); await post('/api/shot/'+sid,{chosen_variation:idx}); toast(sid+' → variation '+(idx+1)); }

async function regen(sid,btn){
  const be=document.getElementById('be-'+sid).value;
  const cost = be==='nano2' ? 'Nano Banana 2 (~$0.15/still \\u00d7 3 \\u2248 $0.45)'
                            : 'flux-general (~$0.04/still \\u00d7 3 \\u2248 $0.12)';
  if(!confirm('\\u26A0 PAID: Regenerate calls '+cost+' via fal and counts against this session\\u2019s limit. Continue?')) return;
  btn.disabled=true; btn.textContent='↻ generating…';
  const {ok,data}=await post('/api/regenerate/'+sid,{backend:be}); btn.disabled=false; btn.textContent='↻ Regenerate';
  if(ok){ toast(sid+' regenerated ('+data.regen_used+'/'+data.regen_limit+', '+data.backend+')'); setTimeout(()=>location.reload(),500);}
  else { toast((data&&data.error)?data.error:'regen failed'); } }

async function approve(){ const {ok,data}=await post('/api/approve');
  if(ok){ toast(data.gate_cleared?'Approved — paid stage unlocked':'Approved'); setTimeout(()=>location.reload(),700); }
  else { alert('Cannot approve yet:\\n'+(data.error||'')+'\\n'+(data.scenes||[]).join(', ')); } }

async function selectProject(rel){ const {ok}=await post('/api/project/select',{rel:rel});
  if(ok){ location.reload(); } else { toast('could not open project'); } }

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
  const d=document.createElement('div'); d.className='msg '+(role==='user'?'u':'a');
  d.textContent=(role==='user'?'You: ':'Vesper: ')+text; l.appendChild(d); l.scrollTop=l.scrollHeight; }
async function chatSend(){ const inp=document.getElementById('chatinput'); const text=inp.value.trim(); if(!text) return;
  inp.value=''; logMsg('user',text); chat.push({role:'user',content:text});
  const {ok,data}=await post('/chat/develop',{messages:chat});
  if(ok){ logMsg('assistant',data.reply); chat.push({role:'assistant',content:data.reply}); }
  else { logMsg('assistant','[error] '+(data.error||'failed')); } }

async function scriptFromChat(){
  if(!chat.length){ toast('chat with Vesper first'); return; }
  if(!confirm('\\u26A0 DESTRUCTIVE: turn this conversation into a NEW storyboard, OVERWRITING the active project (all shot text, knobs, chosen drafts, uploaded reference links). Continue?')) return;
  const beats=document.getElementById('gen_beats').value;
  toast('writing script from chat\\u2026');
  const {ok,data}=await post('/api/script/from_chat',{messages:chat,beats:beats||null});
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
  toast('drafting…'); const {ok,data}=await post('/api/script/generate',{topic:topic,beats:beats||null});
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
    const el=document.getElementById('st-'+k), b=document.getElementById('btn-'+k);
    if(el){ el.textContent=v.status+(v.status==='error'?' \\u2014 check terminal':'');
      el.style.color = v.status==='error'?'#c66':(v.status==='done'?'#7a3':'var(--amber)'); }
    if(b){ b.disabled=(v.status==='running'); }
    if(v.status==='running') running=true;
    if(_lastStatus[k]==='running' && v.status!=='running') justFinished=true;
    _lastStatus[k]=v.status; }
  if(running) setTimeout(pollAssemble,2500);
  else if(justFinished) setTimeout(()=>location.reload(),800); }  // show new clips/preview
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
        default_negative=NEGATIVE_PROMPT, backends=BACKENDS,
        heroes=sum(1 for s in sb.shots if getattr(s, "flow_hero", False)),
        shot_clips=shot_clips, preview_url=preview_url,
        fcpxml_ready=fcpxml_ready, ep_slug=ep["slug"],
        motion_suggest={s.scene_id: _suggest_motion_prompt(s) for s in sb.shots},
    )


@app.get("/assets/<scene>/<path:filename>")
def asset(scene: str, filename: str):
    return send_from_directory(str(config.ASSETS / scene), filename)


@app.get("/references/<path:filename>")
def reference_file(filename: str):
    return send_from_directory(str(config.REFERENCES_DIR), filename)


@app.get("/render/<path:filename>")
def render_file(filename: str):
    return send_from_directory(str(config.RENDER_DIR), filename)


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
