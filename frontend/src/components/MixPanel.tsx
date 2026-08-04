"use client";

import React, { useState, useEffect } from "react";
import { SlidersHorizontal, Save, Info } from "lucide-react";

export interface MixConfig {
  narration: number;   // linear gain, 1.0 = unity
  sfx: number;
  music: number;
}

interface MixPanelProps {
  mix: MixConfig | null;
  /** POST /api/mix */
  onSave: (mix: MixConfig) => Promise<any>;
}

// The manifest stores linear gain; engineers think in dB, and the Track Mixer in
// step2_audio_studio.jpg is labelled in dB. Convert at the edge.
const toDb = (g: number) => (g <= 0.0001 ? -60 : 20 * Math.log10(g));
const fromDb = (db: number) => (db <= -59.5 ? 0 : Math.pow(10, db / 20));

const TRACKS: { key: keyof MixConfig; label: string; source: string }[] = [
  { key: "narration", label: "A1 Narration", source: "ElevenLabs · Vesper" },
  { key: "sfx", label: "A2 SFX", source: "fal · stable-audio" },
  { key: "music", label: "A3 Music Bed", source: "audio_pool" },
];

export default function MixPanel({ mix: initial, onSave }: MixPanelProps) {
  const [mix, setMix] = useState<MixConfig | null>(initial);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setMix(initial); setDirty(false); }, [initial]);

  if (!mix) {
    return <div className="p-4 text-xs text-zinc-500 font-mono">Loading mix…</div>;
  }

  const set = (k: keyof MixConfig, db: number) => {
    setMix({ ...mix, [k]: fromDb(db) });
    setDirty(true);
  };

  const save = async () => {
    setSaving(true);
    await onSave(mix);
    setSaving(false);
    setDirty(false);
  };

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <SlidersHorizontal className="h-4 w-4 text-amber-500" />
          Track Mixer
        </h3>
        <button
          onClick={save}
          disabled={saving || !dirty}
          className="bg-amber-500 hover:bg-amber-600 disabled:opacity-40 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1 shadow-md shadow-amber-500/10"
        >
          <Save className="h-3.5 w-3.5" />
          {saving ? "Saving…" : "Apply"}
        </button>
      </div>

      <div className="flex flex-col gap-3">
        {TRACKS.map((t) => {
          const db = toDb(mix[t.key]);
          return (
            <div key={t.key} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between">
                <span className="text-[11px] font-mono text-zinc-300 font-bold">{t.label}</span>
                <span className="text-[10px] font-mono text-zinc-600">{t.source}</span>
                <span className={`text-[11px] font-mono font-bold tabular-nums ${
                  db > -1 ? "text-emerald-400" : db > -10 ? "text-amber-400" : "text-zinc-400"
                }`}>
                  {db <= -59.5 ? "−∞" : `${db >= 0 ? "+" : ""}${db.toFixed(1)}`} dB
                </span>
              </div>
              <input
                type="range" min={-30} max={6} step={0.5}
                value={Math.max(-30, Math.min(6, db))}
                onChange={(e) => set(t.key, parseFloat(e.target.value))}
                className="w-full accent-amber-500"
              />
            </div>
          );
        })}
      </div>

      <p className="text-[11px] text-zinc-500 leading-relaxed flex gap-1.5">
        <Info className="h-3.5 w-3.5 shrink-0 mt-0.5 text-zinc-600" />
        <span>
          Documentary ambience normally sits 15–25 dB under narration. SFX shipped
          at −5 dB once, which is why foley sat on top of the voice. Re-run the
          preview in Step 5 to hear a change.
        </span>
      </p>

      {dirty && (
        <p className="text-[11px] text-amber-500 font-mono">Unsaved — Apply, then rebuild the preview.</p>
      )}
    </div>
  );
}
