"use client";

import React, { useState, useEffect } from "react";
import { Sun, Save, Info, Lightbulb } from "lucide-react";

export interface Grade {
  brightness: number;
  contrast: number;
  temperature: number;
  saturation: number;
  rim_light: number;
  key_light: string;
  key_intensity: number;
}

interface GradePanelProps {
  grade: Grade | null;
  channel: string;
  onSave: (g: Partial<Grade>) => Promise<any>;
}

const KEYS = ["", "left", "right", "top", "front"];

export default function GradePanel({ grade: initial, channel, onSave }: GradePanelProps) {
  const [g, setG] = useState<Grade | null>(initial);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setG(initial); setDirty(false); }, [initial]);
  if (!g) return <div className="p-4 text-xs text-zinc-500 font-mono">Loading grade…</div>;

  const set = (k: keyof Grade, v: number | string) => { setG({ ...g, [k]: v } as Grade); setDirty(true); };
  const save = async () => { setSaving(true); await onSave(g); setSaving(false); setDirty(false); };

  // Directional relighting only reads on photographic plates. On an illustrated
  // medium the light is painted into the artwork and the depth map is nearly
  // featureless — measured on a real Bestiary plate, a key pushed to double
  // strength barely registered. Showing the control there would be a lie.
  const lightingUseful = channel !== "bestiary";

  const Slider = ({ k, label, min, max, step, fmt }: {
    k: keyof Grade; label: string; min: number; max: number; step: number;
    fmt?: (v: number) => string;
  }) => (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <label className="text-[11px] font-mono text-zinc-400">{label}</label>
        <span className="text-[11px] font-mono text-amber-500 font-bold tabular-nums">
          {fmt ? fmt(g[k] as number) : (g[k] as number).toFixed(2)}
        </span>
      </div>
      <input type="range" min={min} max={max} step={step} value={g[k] as number}
             onChange={(e) => set(k, parseFloat(e.target.value))}
             className="w-full accent-amber-500" />
    </div>
  );

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <Sun className="h-4 w-4 text-amber-500" />
          Grade — Episode Look
        </h3>
        <button onClick={save} disabled={saving || !dirty}
          className="bg-amber-500 hover:bg-amber-600 disabled:opacity-40 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1">
          <Save className="h-3.5 w-3.5" />{saving ? "Saving…" : "Apply"}
        </button>
      </div>

      <Slider k="brightness" label="Brightness" min={-2} max={2} step={0.05}
              fmt={(v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)} stops`} />
      <Slider k="contrast" label="Contrast" min={-0.8} max={0.8} step={0.02} />
      <Slider k="temperature" label="Colour Temperature" min={2000} max={10000} step={100}
              fmt={(v) => `${v} K${v < 5600 ? " · warm" : v > 5600 ? " · cool" : ""}`} />
      <Slider k="saturation" label="Saturation" min={0} max={2} step={0.05} />

      <div className="border-t border-zinc-800 pt-3 flex flex-col gap-3">
        <div className="flex items-center gap-2 text-[11px] font-mono text-zinc-400">
          <Lightbulb className="h-3.5 w-3.5 text-amber-500" />
          Depth Lighting
          <span className="text-zinc-600">· from the parallax depth map, $0</span>
        </div>

        <Slider k="rim_light" label="Rim Light (edge separation)" min={0} max={1} step={0.02} />

        {lightingUseful ? (
          <>
            <div>
              <label className="block text-[11px] font-mono text-zinc-400 mb-1">Key Light</label>
              <div className="flex gap-1">
                {KEYS.map((k) => (
                  <button key={k || "none"} onClick={() => set("key_light", k)}
                    className={`flex-1 px-2 py-1 rounded text-[11px] font-mono border transition ${
                      g.key_light === k
                        ? "bg-amber-500 border-amber-400 text-zinc-950 font-bold"
                        : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:text-zinc-200"}`}>
                    {k || "off"}
                  </button>
                ))}
              </div>
            </div>
            {g.key_light && <Slider k="key_intensity" label="Key Intensity" min={0} max={1} step={0.02} />}
          </>
        ) : (
          <p className="text-[10px] text-zinc-500 leading-relaxed flex gap-1.5">
            <Info className="h-3.5 w-3.5 shrink-0 mt-0.5 text-zinc-600" />
            <span>
              Directional key light is hidden on <span className="text-zinc-400">{channel}</span>:
              these plates are woodblock and manuscript media with the light already
              painted in, and the depth map has almost no surface to shade. Tested on a
              real plate, a key at double strength barely registered. Set light direction
              in the shot prompt instead — the image model can actually paint it.
            </span>
          </p>
        )}
      </div>

      {dirty && (
        <p className="text-[11px] text-amber-500 font-mono">
          Unsaved — Apply, then re-render a beat to see it (Step 4 · Test).
        </p>
      )}
    </div>
  );
}
