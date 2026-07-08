"""Dashboard: the Flask Storyboard/Budget gate (Gate 1).

A local web UI to review generated draft variations, pick/regenerate a frame per
beat, set each beat's motion tier (allocating the paid-render budget), refine
narration, and Approve — which writes storyboard_approved into the manifest and
clears the gate that unlocks the paid video stage.

Run:
    python -m src.dashboard
    -> open http://127.0.0.1:5000
"""

from __future__ import annotations

from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory

from . import config
from .manifest import MotionType, load, save

app = Flask(__name__)

DEFAULT_VIDEO_MODEL = "fal-ai/kling-video/v3/image-to-video"
TIER_LABEL = {"static": "A · still + FX ($0)", "parallax": "B · parallax ($0)",
              "ai_video": "C · AI video (paid)"}

PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>{{ sb.title }} — Storyboard Gate</title>
<style>
  :root { --bg:#14110e; --card:#1e1a15; --line:#3a3128; --amber:#e0a458; --ink:#d8cdbd; --dim:#8a7c68; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:10; background:#100d0a; border-bottom:1px solid var(--line);
    padding:14px 22px; display:flex; align-items:center; gap:20px; }
  header h1 { font-size:17px; margin:0; color:var(--amber); font-weight:600; }
  .meta { color:var(--dim); font-size:13px; }
  .spacer { flex:1; }
  button { font:inherit; cursor:pointer; border:1px solid var(--line); background:var(--card);
    color:var(--ink); padding:7px 12px; border-radius:6px; }
  button:hover { border-color:var(--amber); }
  .approve { background:var(--amber); color:#14110e; border-color:var(--amber); font-weight:600; padding:8px 16px; }
  main { max-width:1180px; margin:0 auto; padding:22px; }
  .beat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px;
    margin-bottom:18px; }
  .beat.approved { border-color:#4a7a3a; }
  .beat-top { display:flex; gap:14px; align-items:flex-start; margin-bottom:12px; }
  .sid { font-family:ui-monospace,monospace; color:var(--amber); font-size:13px; padding-top:6px; min-width:44px; }
  textarea { width:100%; background:#14110e; color:var(--ink); border:1px solid var(--line);
    border-radius:6px; padding:8px; font:inherit; resize:vertical; min-height:52px; }
  select { font:inherit; background:#14110e; color:var(--ink); border:1px solid var(--line);
    border-radius:6px; padding:6px; }
  .tierC select { border-color:var(--amber); color:var(--amber); }
  .vars { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:12px; }
  .var { position:relative; border:2px solid transparent; border-radius:8px; overflow:hidden; cursor:pointer;
    background:#0d0b08; aspect-ratio:16/9; }
  .var img { width:100%; height:100%; object-fit:cover; display:block; }
  .var.sel { border-color:var(--amber); }
  .var .tick { position:absolute; top:6px; right:6px; background:var(--amber); color:#14110e;
    border-radius:50%; width:22px; height:22px; text-align:center; line-height:22px; font-weight:700;
    display:none; } .var.sel .tick { display:block; }
  .empty { color:var(--dim); font-style:italic; padding:20px 0; }
  .row { display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  .visual { color:var(--dim); font-size:12.5px; margin-top:6px; }
  #toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#000;
    border:1px solid var(--amber); color:var(--amber); padding:10px 18px; border-radius:8px;
    opacity:0; transition:.2s; pointer-events:none; }
  #toast.show { opacity:1; }
</style></head><body>
<header>
  <h1>{{ sb.title or "Untitled" }}</h1>
  <span class="meta">{{ sb.shots|length }} beats · <span id="paidCount">{{ paid }}</span> paid (Tier C)
    · script {{ "locked" if sb.script_locked else "draft" }}</span>
  <span class="spacer"></span>
  <span class="meta">{{ "APPROVED ✓" if sb.storyboard_approved else "not approved" }}</span>
  <button class="approve" onclick="approve()">Approve storyboard →</button>
</header>
<main>
{% for s in sb.shots %}
  <div class="beat {{ 'approved' if s.approved else '' }} {{ 'tierC' if s.motion_type.value=='ai_video' else '' }}" id="beat-{{ s.scene_id }}">
    <div class="beat-top">
      <div class="sid">{{ s.scene_id }}</div>
      <div style="flex:1">
        <textarea onchange="saveField('{{ s.scene_id }}','narration',this.value)">{{ s.narration }}</textarea>
        <div class="visual">🎨 {{ s.prompt }}</div>
      </div>
      <select onchange="saveField('{{ s.scene_id }}','motion_type',this.value)">
        {% for v,label in tiers.items() %}
        <option value="{{ v }}" {{ 'selected' if s.motion_type.value==v else '' }}>{{ label }}</option>
        {% endfor %}
      </select>
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
    <div class="empty">No drafts yet — generating…</div>
    {% endif %}
    <div class="row">
      <button onclick="regen('{{ s.scene_id }}',this)">↻ Regenerate (paid)</button>
    </div>
  </div>
{% endfor %}
</main>
<div id="toast"></div>
<script>
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),1800); }
async function post(url,body){ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})}); return {ok:r.ok, data:await r.json()}; }
async function saveField(sid,field,val){ const b={}; b[field]=val; const {data}=await post('/api/shot/'+sid,b);
  if(field==='motion_type'){ document.getElementById('paidCount').textContent=data.paid_count;
    document.getElementById('beat-'+sid).classList.toggle('tierC',val==='ai_video'); } toast(sid+' saved'); }
async function pick(sid,idx,el){ el.parentNode.querySelectorAll('.var').forEach(v=>v.classList.remove('sel'));
  el.classList.add('sel'); await post('/api/shot/'+sid,{chosen_variation:idx}); toast(sid+' → variation '+(idx+1)); }
async function regen(sid,btn){ btn.disabled=true; btn.textContent='↻ generating…';
  const {ok,data}=await post('/api/regenerate/'+sid); btn.disabled=false; btn.textContent='↻ Regenerate (paid)';
  if(ok){ toast(sid+' regenerated'); setTimeout(()=>location.reload(),500);} else { toast('regen failed'); } }
async function approve(){ const {ok,data}=await post('/api/approve');
  if(ok){ toast(data.gate_cleared?'Approved — paid stage unlocked':'Approved'); setTimeout(()=>location.reload(),700); }
  else { alert('Cannot approve yet:\\n'+(data.error||'')+'\\n'+(data.scenes||[]).join(', ')); } }
</script></body></html>
"""


def _paid_count(sb) -> int:
    return len(sb.paid_shots())


@app.get("/")
def index():
    sb = load()
    return render_template_string(PAGE, sb=sb, tiers=TIER_LABEL, paid=_paid_count(sb))


@app.get("/assets/<scene>/<path:filename>")
def asset(scene: str, filename: str):
    return send_from_directory(str(config.ASSETS / scene), filename)


@app.post("/api/shot/<scene_id>")
def update_shot(scene_id: str):
    sb = load()
    shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
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

    save(sb)
    return jsonify(ok=True, paid_count=_paid_count(sb))


@app.post("/api/regenerate/<scene_id>")
def regenerate(scene_id: str):
    from . import assets  # lazy: only import fal path when actually used

    sb = load()
    shot = next((s for s in sb.shots if s.scene_id == scene_id), None)
    if not shot:
        abort(404)
    n = int(request.args.get("n", 3))
    try:
        assets.generate_for_shot(shot, n, assets.load_lora())
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    save(sb)
    return jsonify(ok=True, variations=shot.draft_variations)


@app.post("/api/approve")
def approve():
    """The gate: block until every beat has a chosen image, then approve."""
    sb = load()
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
    save(sb)
    return jsonify(ok=True, gate_cleared=sb.gate_cleared(),
                   paid=[s.scene_id for s in sb.paid_shots()])


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    print(f"Storyboard gate: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run(debug=True)
