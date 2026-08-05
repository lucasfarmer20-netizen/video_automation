"use client";

import React, { useState, useRef } from "react";
import { Handle, Position } from "@xyflow/react";
import { Play, Square, RefreshCw, Trash2, Upload } from "lucide-react";

const toDb = (g: number) => (g <= 0.0001 ? -60 : 20 * Math.log10(g));
const fromDb = (db: number) => (db <= -39.5 ? 0 : Math.pow(10, db / 20));

const TONE: Record<string, { chip: string; accent: string }> = {
  narration: { chip: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25", accent: "accent-emerald-500" },
  sfx: { chip: "bg-amber-500/15 text-amber-400 border-amber-500/25", accent: "accent-amber-500" },
};

/** A single audio clip as its own node: narration, or one SFX layer.
 *
 *  Each layer gets a node rather than sharing one — you cannot tune a layer you
 *  cannot address, and the whole point of layering is independent placement. */
export default function AudioNode({ data }: any) {
  const tone = TONE[data.kind] || TONE.sfx;
  const [playing, setPlaying] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);
  // Live values while dragging; committed on release so a drag is one write.
  const [live, setLive] = useState<Record<string, number>>({});

  const val = (k: string, fallback: number) => (k in live ? live[k] : fallback);
  const commit = (k: string, apply: (v: number) => void) => () => {
    if (k in live) { apply(live[k]); setLive((s) => { const n = { ...s }; delete n[k]; return n; }); }
  };

  const toggle = () => {
    if (!data.url) return;
    if (!audio.current) {
      audio.current = new Audio(data.url);
      audio.current.onended = () => setPlaying(false);
    }
    if (playing) { audio.current.pause(); audio.current.currentTime = 0; setPlaying(false); }
    else { audio.current.volume = Math.min(1, data.gain ?? 1); audio.current.play(); setPlaying(true); }
  };

  const Row = ({ label, k, min, max, step, value, fmt, onDone }: any) => (
    <div className="flex items-center gap-1.5">
      <span className="text-[9px] font-mono text-zinc-500 w-11 shrink-0">{label}</span>
      <input
        type="range" min={min} max={max} step={step} value={val(k, value)}
        onChange={(e) => setLive((s) => ({ ...s, [k]: parseFloat(e.target.value) }))}
        onPointerUp={commit(k, onDone)} onKeyUp={commit(k, onDone)} onBlur={commit(k, onDone)}
        className={`flex-1 min-w-0 h-1 ${tone.accent}`}
      />
      <span className="text-[9px] font-mono text-zinc-400 w-12 text-right tabular-nums shrink-0">
        {fmt(val(k, value))}
      </span>
    </div>
  );

  const db = toDb(data.gain ?? 1);

  return (
    <div className="bg-zinc-950/95 border border-zinc-800 rounded-lg p-2.5 w-60 shadow-xl text-zinc-300">
      <Handle type="target" position={Position.Top} className="!w-2 !h-2 !bg-zinc-600 !border-zinc-950" />

      <div className="flex items-center gap-1.5 mb-2">
        <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded border shrink-0 ${tone.chip}`}>
          {data.kind === "narration" ? "VO" : "SFX"}
        </span>
        <span className="text-[10px] font-mono text-zinc-300 truncate flex-1" title={data.prompt || data.label}>
          {data.label || data.prompt || data.scene_id}
        </span>
        {data.url && (
          <button onClick={toggle} className="text-zinc-400 hover:text-zinc-100 shrink-0" title="Audition">
            {playing ? <Square className="h-3 w-3" /> : <Play className="h-3 w-3" />}
          </button>
        )}
      </div>

      {!data.url && (
        <p className="text-[9px] font-mono text-zinc-600 italic mb-2">
          {data.prompt ? "not generated yet" : "no audio"}
        </p>
      )}

      <div className="flex flex-col gap-1">
        <Row label="gain" k="gain" min={-40} max={12} step={0.5}
             value={Math.max(-40, Math.min(12, db))}
             fmt={(v: number) => (v <= -39.5 ? "−∞" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}`)}
             onDone={(v: number) => data.onPatch?.({ gain: fromDb(v) })} />
        <Row label="offset" k="offset" min={-20} max={20} step={0.1}
             value={data.offset ?? 0}
             fmt={(v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}s`}
             onDone={(v: number) => data.onPatch?.({ offset: v })} />
        <Row label="fade in" k="fade_in" min={0} max={8} step={0.1}
             value={data.fade_in ?? 0} fmt={(v: number) => `${v.toFixed(1)}s`}
             onDone={(v: number) => data.onPatch?.({ fade_in: v })} />
        <Row label="fade out" k="fade_out" min={0} max={8} step={0.1}
             value={data.fade_out ?? 0} fmt={(v: number) => `${v.toFixed(1)}s`}
             onDone={(v: number) => data.onPatch?.({ fade_out: v })} />
      </div>

      {(data.offset ?? 0) < 0 && (
        <p className="text-[9px] font-mono text-amber-500/80 mt-1.5">
          starts {Math.abs(data.offset).toFixed(1)}s before the beat
        </p>
      )}

      <div className="flex items-center gap-1 mt-2 pt-2 border-t border-zinc-900">
        {data.onGenerate && data.prompt && (
          <button onClick={data.onGenerate} title="Regenerate from this layer's prompt"
            className="text-[9px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-100 transition">
            <RefreshCw className="h-2.5 w-2.5" />gen
          </button>
        )}
        {data.onUpload && (
          <button onClick={data.onUpload} title="Replace with your own audio"
            className="text-[9px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-100 transition">
            <Upload className="h-2.5 w-2.5" />file
          </button>
        )}
        {data.source && (
          <span className="text-[9px] font-mono text-zinc-600 ml-auto">{data.source}</span>
        )}
        {data.onDelete && (
          <button onClick={data.onDelete} title="Remove this layer from the mix (the file is kept)"
            className="text-zinc-600 hover:text-red-400 transition shrink-0">
            <Trash2 className="h-3 w-3" />
          </button>
        )}
      </div>
    </div>
  );
}
