"use client";

import React from "react";

/** Trim shown on the clip itself, in dB.
 *
 *  Unity draws nothing — a badge on every clip is noise, and the point is to
 *  see at a glance which beats have been pushed away from the bus level. */
export function GainPill({ gain }: { gain?: number }) {
  const g = gain ?? 1;
  if (Math.abs(g - 1) < 0.01) return null;
  const db = g <= 0.0001 ? -60 : 20 * Math.log10(g);
  return (
    <span
      className={`absolute right-1 top-1 px-1 rounded text-[8px] font-mono font-bold tabular-nums z-10 pointer-events-none ${
        db > 0
          ? "bg-amber-500 text-zinc-950"
          : "bg-zinc-950/85 text-zinc-300 border border-zinc-700"
      }`}
      title={`Trim ${db >= 0 ? "+" : ""}${db.toFixed(1)} dB on top of the bus level`}
    >
      {db <= -39.5 ? "−∞" : `${db >= 0 ? "+" : ""}${db.toFixed(1)}`}
    </span>
  );
}

/** Procedural FX already on the plate (candle flicker, mist, motes).
 *
 *  Shot.fx has always driven the renderer and was never surfaced, so there was
 *  no way to see which beats carry effects without opening each one. */
export function FxPills({ fx, wide }: { fx?: string[]; wide: boolean }) {
  if (!fx?.length || !wide) return null;
  return (
    <span className="absolute left-1 bottom-1 flex gap-0.5 z-10 pointer-events-none">
      {fx.slice(0, 3).map((f) => (
        <span
          key={f}
          className="px-1 rounded bg-zinc-950/80 border border-zinc-700 text-[8px] font-mono text-zinc-400"
          title={f}
        >
          {f.split(/[\s,]/)[0]}
        </span>
      ))}
      {fx.length > 3 && (
        <span className="px-1 rounded bg-zinc-950/80 border border-zinc-700 text-[8px] font-mono text-zinc-500">
          +{fx.length - 3}
        </span>
      )}
    </span>
  );
}
