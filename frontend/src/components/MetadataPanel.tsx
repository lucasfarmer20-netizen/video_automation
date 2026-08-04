"use client";

import React, { useState, useEffect } from "react";
import { FileText, Sparkles, Save, Copy, Check, Package, Download, AlertTriangle } from "lucide-react";

export interface Chapter { start: number; timestamp: string; title: string }

export interface Metadata {
  title: string;
  description: string;
  description_with_chapters: string;
  tags: string[];
  tags_chars: number;
  chapters: Chapter[];
  thumbnail_prompts: string[];
  title_chars: number;
  warnings: string[];
}

interface MetadataPanelProps {
  metadata: Metadata | null;
  busy: boolean;
  bundleBusy: boolean;
  bundleReady: boolean;
  onGenerate: () => void;
  onSave: (md: Partial<Metadata>) => Promise<any>;
  onBuildBundle: () => void;
  /** Absolute URLs for the download links. */
  bundleUrl: string;
  fcpxmlUrl: string;
  fcpxmlReady: boolean;
}

const TITLE_MAX = 100;
const DESC_MAX = 5000;

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={async () => {
        try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); }
        catch { /* clipboard blocked — the field is selectable anyway */ }
      }}
      className="text-[10px] font-mono text-zinc-500 hover:text-amber-400 transition flex items-center gap-1"
      title={`Copy ${label}`}
    >
      {done ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3" />}
      {done ? "copied" : "copy"}
    </button>
  );
}

export default function MetadataPanel({
  metadata: initial, busy, bundleBusy, bundleReady,
  onGenerate, onSave, onBuildBundle, bundleUrl, fcpxmlUrl, fcpxmlReady
}: MetadataPanelProps) {
  const [title, setTitle] = useState(initial?.title ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [tags, setTags] = useState((initial?.tags ?? []).join(", "));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setTitle(initial?.title ?? "");
    setDescription(initial?.description ?? "");
    setTags((initial?.tags ?? []).join(", "));
    setDirty(false);
  }, [initial]);

  const save = async () => {
    setSaving(true);
    await onSave({
      title,
      description,
      tags: tags.split(",").map((t) => t.trim().replace(/^#/, "").toLowerCase()).filter(Boolean),
      // Chapters are computed from the manifest, never edited here — a hand-typed
      // timestamp would drift from the cut the moment a duration changes.
      chapters: initial?.chapters ?? [],
      thumbnail_prompts: initial?.thumbnail_prompts ?? [],
    });
    setSaving(false);
    setDirty(false);
  };

  const tagChars = tags.length;

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 flex flex-col">
      <div className="px-4 py-3 border-b border-zinc-900 flex items-center justify-between gap-2 flex-wrap">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <FileText className="h-4 w-4 text-amber-500" />
          Publishing Metadata
        </h3>
        <div className="flex items-center gap-2">
          {dirty && (
            <button
              onClick={save}
              disabled={saving}
              className="bg-amber-500 hover:bg-amber-600 disabled:opacity-40 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1"
            >
              <Save className="h-3.5 w-3.5" />{saving ? "Saving…" : "Save"}
            </button>
          )}
          <button
            onClick={onGenerate}
            disabled={busy}
            className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 text-zinc-100 border border-zinc-700 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1.5"
          >
            <Sparkles className="h-3.5 w-3.5 text-amber-500" />
            {busy ? "Drafting…" : initial ? "Re-draft" : "Draft with Vesper"}
          </button>
        </div>
      </div>

      {!initial && !busy ? (
        <div className="p-4 text-xs text-zinc-500 leading-relaxed">
          No metadata drafted yet. Vesper writes the title, description, chapter
          titles and tags from the locked script — chapter timestamps come from the
          manifest&apos;s own beat durations, so they always match the cut.
        </div>
      ) : (
        <div className="p-4 flex flex-col gap-4">
          {initial?.warnings?.length ? (
            <div className="bg-amber-950/25 border border-amber-500/30 rounded-lg px-3 py-2 text-[11px] text-amber-200/90 font-mono flex flex-col gap-1">
              {initial.warnings.map((w, i) => (
                <span key={i} className="flex gap-1.5">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px text-amber-400" />{w}
                </span>
              ))}
            </div>
          ) : null}

          <div>
            <div className="flex items-baseline justify-between mb-1">
              <label className="text-[11px] font-mono text-zinc-400">Title</label>
              <div className="flex items-center gap-3">
                <span className={`text-[10px] font-mono tabular-nums ${title.length > TITLE_MAX ? "text-red-400" : "text-zinc-600"}`}>
                  {title.length}/{TITLE_MAX}
                </span>
                <CopyButton text={title} label="title" />
              </div>
            </div>
            <input
              value={title}
              onChange={(e) => { setTitle(e.target.value); setDirty(true); }}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-xs"
            />
          </div>

          <div>
            <div className="flex items-baseline justify-between mb-1">
              <label className="text-[11px] font-mono text-zinc-400">Description</label>
              <div className="flex items-center gap-3">
                <span className={`text-[10px] font-mono tabular-nums ${description.length > DESC_MAX ? "text-red-400" : "text-zinc-600"}`}>
                  {description.length}/{DESC_MAX}
                </span>
                <CopyButton text={initial?.description_with_chapters || description} label="description with chapters" />
              </div>
            </div>
            <textarea
              value={description}
              onChange={(e) => { setDescription(e.target.value); setDirty(true); }}
              rows={7}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-xs leading-relaxed resize-y"
            />
            <p className="text-[10px] text-zinc-600 mt-1">
              Copy sends the description <em>with</em> the chapter list appended.
            </p>
          </div>

          <div>
            <div className="flex items-baseline justify-between mb-1">
              <label className="text-[11px] font-mono text-zinc-400">Tags (comma separated)</label>
              <div className="flex items-center gap-3">
                <span className={`text-[10px] font-mono tabular-nums ${tagChars > 500 ? "text-red-400" : "text-zinc-600"}`}>
                  {tagChars}/500
                </span>
                <CopyButton text={tags} label="tags" />
              </div>
            </div>
            <input
              value={tags}
              onChange={(e) => { setTags(e.target.value); setDirty(true); }}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-xs font-mono"
            />
          </div>

          {initial?.chapters?.length ? (
            <div>
              <label className="text-[11px] font-mono text-zinc-400 block mb-1">
                Chapters <span className="text-zinc-600">— timecodes computed from beat durations</span>
              </label>
              <div className="bg-zinc-900/60 border border-zinc-800 rounded-lg p-2 max-h-40 overflow-y-auto grid grid-cols-2 gap-x-4 gap-y-0.5">
                {initial.chapters.map((c) => (
                  <div key={c.start} className="text-[11px] font-mono flex gap-2">
                    <span className="text-amber-500 tabular-nums">{c.timestamp}</span>
                    <span className="text-zinc-400 truncate">{c.title}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {initial?.thumbnail_prompts?.length ? (
            <div>
              <label className="text-[11px] font-mono text-zinc-400 block mb-1">Thumbnail prompts</label>
              <div className="flex flex-col gap-1">
                {initial.thumbnail_prompts.map((p, i) => (
                  <div key={i} className="text-[11px] text-zinc-400 bg-zinc-900/60 border border-zinc-800 rounded px-2 py-1 flex items-start gap-2">
                    <span className="flex-1 leading-relaxed">{p}</span>
                    <CopyButton text={p} label="prompt" />
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      )}

      {/* Export — the bundle is the thing that actually opens elsewhere. */}
      <div className="px-4 py-3 border-t border-zinc-900 flex flex-col gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={onBuildBundle}
            disabled={bundleBusy}
            className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 text-zinc-100 border border-zinc-700 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1.5"
          >
            <Package className="h-3.5 w-3.5 text-amber-500" />
            {bundleBusy ? "Packing…" : "Build export bundle"}
          </button>
          {bundleReady && (
            <a
              href={bundleUrl}
              className="bg-amber-500 hover:bg-amber-600 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1.5"
            >
              <Download className="h-3.5 w-3.5" />Download bundle (.zip)
            </a>
          )}
          {fcpxmlReady && (
            <a href={fcpxmlUrl} className="text-[11px] font-mono text-zinc-500 hover:text-zinc-300 underline underline-offset-2">
              .fcpxml only
            </a>
          )}
        </div>
        <p className="text-[10px] text-zinc-600 leading-relaxed">
          The bare .fcpxml references media by absolute server path, so on any
          other machine every clip is offline. The bundle rewrites those paths
          relative and ships the media beside them — unzip and import.
        </p>
      </div>
    </div>
  );
}
