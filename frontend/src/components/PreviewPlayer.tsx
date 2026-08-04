"use client";

import React from "react";

interface PreviewPlayerProps {
  previewUrl: string | null;
  fcpxmlReady: boolean;
  epSlug: string;
  mediaUrl: (path: string) => string;
}

/** The server-rendered preview cut. Rendering always happens server-side from
 *  the manifest — there is deliberately no browser compositor, because it would
 *  drift from the real renderer and cannot reproduce the depth-warp parallax. */
export default function PreviewPlayer({ previewUrl, fcpxmlReady, epSlug, mediaUrl }: PreviewPlayerProps) {
  if (!previewUrl) {
    return (
      <div className="border-t border-zinc-900 pt-4 mt-2 text-xs text-zinc-500 font-mono">
        No preview built yet. Run <span className="text-amber-500">Build preview</span> once beats are rendered.
      </div>
    );
  }
  return (
    <div className="border-t border-zinc-900 pt-4 mt-2">
      <video
        controls
        className="w-full max-h-80 bg-black border border-zinc-900 rounded-lg shadow-2xl"
        src={mediaUrl(previewUrl)}
      />
      <div className="text-[10px] text-zinc-500 font-mono mt-2 flex justify-between select-none">
        <span>Narration + SFX + music synced proxy track</span>
        {fcpxmlReady && (
          <span className="text-amber-500 font-semibold">
            ▶ Timeline compiled — import {epSlug}.fcpxml into DaVinci Resolve
          </span>
        )}
      </div>
    </div>
  );
}
