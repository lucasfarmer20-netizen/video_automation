"use client";

import React, { useEffect, useMemo, useRef } from "react";
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  Edge
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import AudioNode from "./AudioNode";

interface Shot {
  scene_id: string;
  narration: string;
  motion_type: string;
  camera: { duration: number; duration_locked?: boolean };
  draft_image: string | null;
  video_clip: string | null;
  image_model?: string | null;
  video_model_key?: string | null;
  draft_variations?: string[];
  chosen_variation?: number | null;
  sfx?: string;
  has_narration?: boolean;
  has_sfx?: boolean;
  narration_url?: string | null;
  sfx_url?: string | null;
  gain_narration?: number;
  gain_sfx?: number;
  offset_narration?: number;
  fade_in_narration?: number;
  fade_out_narration?: number;
  sfx_layers_resolved?: Array<{
    id: string; prompt?: string; label?: string; source?: string;
    gain?: number; offset?: number; fade_in?: number; fade_out?: number;
    url?: string | null;
  }>;
}

interface FlowCanvasProps {
  shots: Shot[];
  mediaUrl: (path: string) => string;
  onUpdateDuration?: (sceneId: string, duration: number) => void;
  onRegenerate?: (sceneId: string) => void;
  onGenerateSFX?: (sceneId: string) => void;
  onRegenNarration?: (sceneId: string) => void;
  /** Opens the full-size take viewer for this beat. */
  onOpenImage?: (sceneId: string, images: string[], idx: number, chosen: number | null) => void;
  /** POST /api/shot/{id} for narration, /layers for a layer. */
  onPatchNarration?: (sceneId: string, patch: Record<string, number>) => void;
  onPatchLayer?: (sceneId: string, layerId: string, patch: Record<string, number>) => void;
  onGenerateLayer?: (sceneId: string, layerId: string) => void;
  onDeleteLayer?: (sceneId: string, layerId: string) => void;
  onUploadLayer?: (sceneId: string) => void;
  /** POST /api/shot/{id} with { gain_narration } or { gain_sfx }. */
  onUpdateGain?: (sceneId: string, field: "gain_narration" | "gain_sfx", v: number) => void;
  /** Registry label maps, so a badge never shows a model the resolver disowns. */
  imageBackends?: Record<string, string>;
  videoBackends?: Record<string, string>;
  /** Project-level image backend, used when a beat has no override. */
  defaultImageModel?: string;
}

const MOTION_STYLE: Record<string, string> = {
  parallax: "bg-blue-500/15 text-blue-400 border-blue-500/25",
  ai_video: "bg-purple-500/15 text-purple-400 border-purple-500/25",
  static: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

const toDb = (g: number) => (g <= 0.0001 ? -60 : 20 * Math.log10(g));
const fromDb = (db: number) => (db <= -39.5 ? 0 : Math.pow(10, db / 20));

const TRACK_COLOUR: Record<string, string> = {
  emerald: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  amber: "bg-amber-500/15 text-amber-400 border-amber-500/25",
};

/** One audio track on a beat node: audition it, and trim it against the bus. */
function AudioRow({ label, colour, url, present, gain, onGain, absent, hint, onRegen }: any) {
  const [playing, setPlaying] = React.useState(false);
  const ref = React.useRef<HTMLAudioElement | null>(null);
  // Local while dragging, committed on release. A range input fires onChange for
  // every pixel of travel, and each commit is a POST -> save_current_project ->
  // GCS write; dragging across the slider would be ~100 writes.
  const [local, setLocal] = React.useState<number | null>(null);
  const db = local ?? toDb(gain ?? 1);
  const commit = () => {
    if (local !== null) { onGain?.(fromDb(local)); setLocal(null); }
  };

  const toggle = () => {
    if (!url) return;
    if (!ref.current) {
      ref.current = new Audio(url);
      ref.current.onended = () => setPlaying(false);
    }
    if (playing) { ref.current.pause(); ref.current.currentTime = 0; setPlaying(false); }
    else { ref.current.volume = Math.min(1, gain ?? 1); ref.current.play(); setPlaying(true); }
  };

  return (
    <div className="flex items-center gap-1.5 text-[9px] font-mono">
      <span className={`px-1.5 py-0.5 rounded border shrink-0 ${TRACK_COLOUR[colour]}`}>{label}</span>
      {present ? (
        <>
          <button
            onClick={toggle}
            title={hint || "Audition this clip"}
            className="text-zinc-400 hover:text-zinc-100 transition shrink-0 w-4"
          >
            {playing ? "■" : "▶"}
          </button>
          <input
            type="range" min={-40} max={12} step={0.5}
            value={Math.max(-40, Math.min(12, db))}
            onChange={(e) => setLocal(parseFloat(e.target.value))}
            onPointerUp={commit}
            onKeyUp={commit}
            onBlur={commit}
            className="flex-1 min-w-0 accent-amber-500 h-1"
            title="Trim on top of the episode bus level"
          />
          <span className={`w-12 text-right tabular-nums shrink-0 ${
            Math.abs(db) < 0.25 ? "text-zinc-600" : db > 0 ? "text-amber-400" : "text-zinc-400"}`}>
            {db <= -39.5 ? "−∞" : `${db >= 0 ? "+" : ""}${db.toFixed(1)}`}
          </span>
        </>
      ) : (
        <span className="text-zinc-600 italic truncate flex-1">{absent}</span>
      )}
      {onRegen && (
        <button onClick={onRegen} title="Regenerate this clip"
          className="shrink-0 text-zinc-500 hover:text-amber-400 transition">↻</button>
      )}
    </div>
  );
}

const BeatNode = ({ data }: any) => {
  const locked = !!data.duration_locked;
  return (
    <div className="bg-zinc-950/90 border border-zinc-800 rounded-xl p-3.5 w-72 shadow-2xl glass-panel text-zinc-300 relative group">
      <Handle type="target" position={Position.Left} className="!w-2.5 !h-2.5 !bg-amber-500 !border-zinc-950" />

      <div className="flex items-center justify-between border-b border-zinc-900 pb-2 mb-2.5">
        <span className="text-[10px] font-bold text-amber-500 font-mono">{data.scene_id}</span>
        <div className="flex items-center gap-1">
          {data.onRegenerate && (
            <button
              onClick={() => data.onRegenerate(data.scene_id)}
              className="text-[9px] bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 px-1.5 py-0.5 rounded border border-amber-500/20 transition font-mono"
              title="Regenerate still"
            >
              🔄
            </button>
          )}
          {data.onGenerateSFX && (
            <button
              onClick={() => data.onGenerateSFX(data.scene_id)}
              className="text-[9px] bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 px-1.5 py-0.5 rounded border border-blue-500/20 transition font-mono"
              title="Generate SFX"
            >
              🔊
            </button>
          )}
          <span className={`text-[9px] uppercase tracking-wider px-2 py-0.5 rounded border font-mono font-semibold ${
            MOTION_STYLE[data.motion_type] || MOTION_STYLE.static
          }`}>
            {data.motion_type}
          </span>
        </div>
      </div>

      {data.thumbnail ? (
        <img
          src={data.thumbnail}
          onClick={() => data.onOpenImage?.(
            data.scene_id,
            data.variations?.length ? data.variations : [],
            Math.max(0, data.chosen ?? 0),
            data.chosen ?? null,
          )}
          title="Click to view full size"
          className="w-full h-28 object-cover rounded-lg border border-zinc-900 mb-2.5 cursor-zoom-in hover:border-amber-500/50 transition"
          alt="node preview"
        />
      ) : (
        <div className="w-full h-28 bg-zinc-900/40 rounded-lg border border-zinc-900/80 mb-2.5 flex items-center justify-center text-[10px] text-zinc-600 italic">
          No image draft
        </div>
      )}

      {/* Model badges, as in 00_dashboard_overview.jpg. Labels come from the
          backend registries, so a badge can never name a model the resolver
          would silently substitute. */}
      {(data.imageLabel || data.videoLabel) && (
        <div className="flex flex-wrap gap-1 mb-2">
          {data.imageLabel && (
            <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400" title="Image model">
              {data.imageLabel}
            </span>
          )}
          {data.videoLabel && (
            <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-purple-500/10 border border-purple-500/25 text-purple-300" title="Video model (paid tier)">
              {data.videoLabel}
            </span>
          )}
        </div>
      )}

      <p className="text-[10px] text-zinc-400 line-clamp-2 leading-relaxed mb-2 font-medium">
        {data.narration || "No voiceover narration script..."}
      </p>

      {/* Audio, split by track. Each carries its own trim because the raw stems
          vary ~13 dB between beats -- the episode fader sets the bus, these seat
          individual beats against it. */}
      <div className="border-t border-zinc-900/60 pt-2 mb-2 flex flex-col gap-1.5">
        <AudioRow
          label="A1 VO" colour="emerald"
          url={data.narrationUrl} present={data.hasNarration}
          gain={data.gainNarration}
          onGain={(v: number) => data.onUpdateGain?.(data.scene_id, "gain_narration", v)}
          absent="not generated"
          onRegen={data.onRegenNarration ? () => data.onRegenNarration(data.scene_id) : undefined}
        />
        <AudioRow
          label="A2 SFX" colour="amber"
          url={data.sfxUrl} present={data.hasSfx}
          gain={data.gainSfx}
          onGain={(v: number) => data.onUpdateGain?.(data.scene_id, "gain_sfx", v)}
          absent={data.sfxPrompt ? "not generated" : "none for this beat"}
          hint={data.sfxPrompt}
          onRegen={data.sfxPrompt && data.onGenerateSFX ? () => data.onGenerateSFX(data.scene_id) : undefined}
        />
      </div>

      <div className="flex items-center gap-1.5 mt-2 border-t border-zinc-900/60 pt-2 text-[9px] font-mono select-none">
        <span className={`w-1.5 h-1.5 rounded-full ${data.has_video ? "bg-emerald-500 shadow-[0_0_8px_#10b981]" : "bg-zinc-700"}`}></span>
        <span className={data.has_video ? "text-zinc-300" : "text-zinc-500"}>
          {data.has_video ? "Clip Rendered" : "No Video"}
        </span>

        <div className="ml-auto flex items-center gap-1 text-zinc-400">
          <span title={locked ? "Duration locked against narration sync" : "Duration follows narration"}>
            {locked ? "🔒" : "⏱"}
          </span>
          <input
            type="number"
            step="0.1"
            min="1"
            max="60"
            // defaultValue + onBlur, not value + onChange. This used to POST on
            // every keystroke: typing "19.0" was four writes to /api/shot/{id},
            // each one a save_current_project -> GCS write.
            defaultValue={data.duration}
            key={`${data.scene_id}-${data.duration}`}
            onBlur={(e) => {
              const v = parseFloat(e.target.value);
              if (!Number.isFinite(v) || Math.abs(v - data.duration) < 0.05) return;
              data.onUpdateDuration?.(data.scene_id, v);
            }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
            className="w-12 bg-zinc-900 text-amber-400 text-right px-1 py-0.5 rounded border border-zinc-800 text-[9px] font-mono focus:outline-none focus:border-amber-400"
          />
          <span>s</span>
        </div>
      </div>

      <Handle type="source" position={Position.Right} className="!w-2.5 !h-2.5 !bg-amber-500 !border-zinc-950" />
    </div>
  );
};

export default function FlowCanvas({
  shots, mediaUrl, onUpdateDuration, onRegenerate, onGenerateSFX,
  imageBackends, videoBackends, defaultImageModel, onUpdateGain, onRegenNarration, onOpenImage,
  onPatchNarration, onPatchLayer, onGenerateLayer, onDeleteLayer, onUploadLayer
}: FlowCanvasProps) {
  const nodeTypes = useMemo(() => ({ beatNode: BeatNode, audioNode: AudioNode }), []);
  const [nodes, setNodes, onNodesChange] = useNodesState<any>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<any>([]);
  // Node layout is regenerated whenever `shots` changes, which would otherwise
  // throw away any node the user has dragged. Remember placements by scene_id.
  const positions = useRef<Record<string, { x: number; y: number }>>({});

  useEffect(() => {
    const COL = 300, VO_Y = 300, SFX_Y = 470, ROW = 190;
    const nodes: any[] = [];
    const edges: Edge[] = [];

    shots.forEach((s, idx) => {
      const x = idx * COL + 50;
      const imgKey = (s.image_model || defaultImageModel || "").trim();
      nodes.push({
        id: s.scene_id,
        type: "beatNode",
        position: positions.current[s.scene_id] ?? { x, y: 60 },
        data: {
          scene_id: s.scene_id, motion_type: s.motion_type, narration: s.narration,
          thumbnail: s.draft_image ? mediaUrl(s.draft_image) : null,
          has_video: !!s.video_clip,
          duration: s.camera.duration, duration_locked: s.camera.duration_locked,
          imageLabel: imgKey ? (imageBackends?.[imgKey] || imgKey) : null,
          videoLabel: s.motion_type === "ai_video" && s.video_model_key
            ? (videoBackends?.[s.video_model_key] || s.video_model_key) : null,
          onUpdateDuration, onRegenerate,
        },
      });

      // Narration as its own node, hung below its beat.
      if ((s.narration || "").trim()) {
        const nid = `${s.scene_id}::vo`;
        nodes.push({
          id: nid, type: "audioNode",
          position: positions.current[nid] ?? { x, y: VO_Y },
          data: {
            kind: "narration", scene_id: s.scene_id, label: `${s.scene_id} narration`,
            url: s.narration_url ? mediaUrl(s.narration_url) : null,
            gain: s.gain_narration ?? 1, offset: s.offset_narration ?? 0,
            fade_in: s.fade_in_narration ?? 0, fade_out: s.fade_out_narration ?? 0,
            prompt: "", source: "",
            onPatch: (patch: Record<string, number>) => {
              // Narration lives on the shot, not in a layer list, so its keys
              // are named differently on the wire.
              const map: Record<string, string> = {
                gain: "gain_narration", offset: "offset_narration",
                fade_in: "fade_in_narration", fade_out: "fade_out_narration",
              };
              const out: Record<string, number> = {};
              for (const [k, v] of Object.entries(patch)) out[map[k] || k] = v;
              onPatchNarration?.(s.scene_id, out);
            },
            onGenerate: onRegenNarration ? () => onRegenNarration(s.scene_id) : undefined,
          },
        });
        edges.push({ id: `e-${nid}`, source: s.scene_id, target: nid, type: "default",
                     style: { stroke: "#10b981", strokeWidth: 1.2, strokeDasharray: "4 3" } });
      }

      // One node per SFX layer -- a layer you cannot address is a layer you
      // cannot tune, which is the whole point of layering.
      (s.sfx_layers_resolved || []).forEach((lay, li) => {
        const lid = `${s.scene_id}::sfx::${lay.id}`;
        nodes.push({
          id: lid, type: "audioNode",
          position: positions.current[lid] ?? { x, y: SFX_Y + li * ROW },
          data: {
            kind: "sfx", scene_id: s.scene_id, layer_id: lay.id,
            label: lay.label || lay.prompt || `layer ${li + 1}`,
            prompt: lay.prompt || "", source: lay.source || "",
            url: lay.url ? mediaUrl(lay.url) : null,
            gain: lay.gain ?? 1, offset: lay.offset ?? 0,
            fade_in: lay.fade_in ?? 0, fade_out: lay.fade_out ?? 0,
            onPatch: (patch: Record<string, number>) => onPatchLayer?.(s.scene_id, lay.id, patch),
            onGenerate: onGenerateLayer ? () => onGenerateLayer(s.scene_id, lay.id) : undefined,
            onDelete: onDeleteLayer ? () => onDeleteLayer(s.scene_id, lay.id) : undefined,
            onUpload: onUploadLayer ? () => onUploadLayer(s.scene_id) : undefined,
          },
        });
        edges.push({ id: `e-${lid}`, source: s.scene_id, target: lid, type: "default",
                     style: { stroke: "#f59e0b", strokeWidth: 1.2, strokeDasharray: "4 3" } });
      });

      if (idx < shots.length - 1) {
        edges.push({
          id: `e-${s.scene_id}-${shots[idx + 1].scene_id}`,
          source: s.scene_id, target: shots[idx + 1].scene_id,
          type: "default", animated: true,
          style: { stroke: "#f59e0b", strokeWidth: 1.5 },
        });
      }
    });

    const generatedNodes = nodes;
    const generatedEdges = edges;
    setNodes(generatedNodes);
    setEdges(generatedEdges);
  }, [shots, mediaUrl, setNodes, setEdges, imageBackends, videoBackends, defaultImageModel,
      onUpdateDuration, onRegenerate, onGenerateSFX, onUpdateGain, onRegenNarration, onOpenImage]);

  return (
    <div className="w-full h-[650px] bg-zinc-950/85 backdrop-blur-md border border-zinc-800/80 rounded-2xl overflow-hidden shadow-2xl">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={(_, node) => { positions.current[node.id] = node.position; }}
        nodeTypes={nodeTypes}
        fitView
        className="text-zinc-200"
      >
        <Controls className="!bg-zinc-900/90 !backdrop-blur-md !border-zinc-800 !text-zinc-200 !rounded-xl !shadow-xl" />
        <Background color="#27272a" gap={20} size={1.2} />
      </ReactFlow>
    </div>
  );
}
