"use client";

import React, { useEffect, useState, useCallback } from "react";
import { createPortal } from "react-dom";
import { X, ChevronLeft, ChevronRight, Check, Trash2 } from "lucide-react";

export interface LightboxState {
  sceneId: string;
  images: string[];        // media-root-relative paths
  index: number;
  chosen: number | null;
  label?: string;
}

interface LightboxProps {
  state: LightboxState | null;
  mediaUrl: (p: string) => string;
  onClose: () => void;
  onIndex: (i: number) => void;
  onChoose?: (sceneId: string, index: number) => void;
  onDelete?: (sceneId: string, index: number) => void;
}

/** Full-size take viewer.
 *
 *  Choosing a render from a 100px-wide thumbnail is guesswork — the differences
 *  between takes are in the faces, the grain and the light, none of which
 *  survive that size. This shows the image at whatever the screen allows and
 *  lets the take be chosen without going back to the grid.
 *
 *  Portalled to <body>: an ancestor with backdrop-filter would otherwise become
 *  the containing block for position:fixed and trap the overlay inside a card.
 */
export default function Lightbox({
  state, mediaUrl, onClose, onIndex, onChoose, onDelete,
}: LightboxProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const n = state?.images.length ?? 0;
  const step = useCallback((d: -1 | 1) => {
    if (!state || n < 2) return;
    onIndex((state.index + d + n) % n);
  }, [state, n, onIndex]);

  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
      else if (e.key === "Enter" && onChoose) {
        e.preventDefault();
        onChoose(state.sceneId, state.index);
      }
    };
    window.addEventListener("keydown", onKey);
    // The page behind must not scroll while this is open.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [state, step, onClose, onChoose]);

  if (!state || !mounted || !n) return null;
  const src = mediaUrl(state.images[state.index]);
  const isChosen = state.chosen === state.index;

  const overlay = (
    <div
      className="fixed inset-0 z-[100] bg-zinc-950/95 backdrop-blur-md flex flex-col"
      onClick={onClose}
    >
      <div className="flex items-center justify-between px-5 py-3 text-xs font-mono text-zinc-400 shrink-0">
        <span className="flex items-center gap-3">
          <span className="text-amber-400 font-bold">{state.sceneId}</span>
          <span>take {state.index + 1} of {n}</span>
          {isChosen && (
            <span className="px-2 py-0.5 rounded-full bg-amber-500 text-zinc-950 font-bold flex items-center gap-1">
              <Check className="h-3 w-3" strokeWidth={3} /> in use
            </span>
          )}
        </span>
        <span className="hidden sm:inline text-zinc-600">
          ←/→ takes · Enter to use · Esc to close
        </span>
        <button onClick={onClose} className="text-zinc-400 hover:text-zinc-100 transition">
          <X className="h-5 w-5" />
        </button>
      </div>

      {/* Clicks on the image itself must not close the overlay. */}
      <div className="flex-1 min-h-0 flex items-center justify-center px-4 pb-2 relative"
           onClick={(e) => e.stopPropagation()}>
        {n > 1 && (
          <button
            onClick={() => step(-1)}
            className="absolute left-3 z-10 p-2 rounded-full bg-zinc-900/80 border border-zinc-700 text-zinc-300 hover:text-white hover:border-amber-400 transition"
            title="Previous take (←)"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
        )}
        <img
          src={src}
          alt={`${state.sceneId} take ${state.index + 1}`}
          className="max-h-full max-w-full object-contain rounded-lg shadow-2xl"
        />
        {n > 1 && (
          <button
            onClick={() => step(1)}
            className="absolute right-3 z-10 p-2 rounded-full bg-zinc-900/80 border border-zinc-700 text-zinc-300 hover:text-white hover:border-amber-400 transition"
            title="Next take (→)"
          >
            <ChevronRight className="h-5 w-5" />
          </button>
        )}
      </div>

      <div className="shrink-0 px-5 py-3 flex items-center justify-center gap-3"
           onClick={(e) => e.stopPropagation()}>
        {onChoose && (
          <button
            onClick={() => onChoose(state.sceneId, state.index)}
            disabled={isChosen}
            className={`px-4 py-2 rounded-xl text-xs font-bold transition flex items-center gap-2 ${
              isChosen
                ? "bg-zinc-800 text-zinc-500 cursor-default"
                : "bg-amber-500 hover:bg-amber-400 text-zinc-950"
            }`}
          >
            <Check className="h-4 w-4" strokeWidth={3} />
            {isChosen ? "This take is in use" : "Use this take"}
          </button>
        )}
        {onDelete && n > 1 && !isChosen && (
          <button
            onClick={() => {
              if (!window.confirm(`Delete take ${state.index + 1} of ${state.sceneId}?`)) return;
              onDelete(state.sceneId, state.index);
              onClose();
            }}
            className="px-3 py-2 rounded-xl text-xs font-bold bg-zinc-900 border border-zinc-700 text-zinc-400 hover:text-red-400 hover:border-red-500/40 transition flex items-center gap-2"
          >
            <Trash2 className="h-3.5 w-3.5" /> Delete take
          </button>
        )}
      </div>

      {/* Filmstrip — the whole point is comparing takes, so keep them reachable. */}
      {n > 1 && (
        <div className="shrink-0 flex items-center justify-center gap-2 px-5 pb-4 overflow-x-auto"
             onClick={(e) => e.stopPropagation()}>
          {state.images.map((p, i) => (
            <button
              key={i}
              onClick={() => onIndex(i)}
              className={`relative h-14 aspect-video rounded overflow-hidden border-2 shrink-0 transition ${
                i === state.index ? "border-amber-400" : "border-zinc-800 opacity-60 hover:opacity-100"
              }`}
            >
              <img src={mediaUrl(p)} alt="" className="w-full h-full object-cover" />
              {state.chosen === i && (
                <span className="absolute top-0.5 right-0.5 bg-amber-500 text-zinc-950 rounded-full w-3.5 h-3.5 flex items-center justify-center">
                  <Check className="h-2.5 w-2.5" strokeWidth={4} />
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  return createPortal(overlay, document.body);
}
