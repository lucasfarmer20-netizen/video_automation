"use client";

import React, { useState, useEffect } from "react";
import { Settings, Save, Trash2, Upload, HelpCircle } from "lucide-react";

interface RenderConfig {
  backend: string;
  video_model: string;
  video_chaining: string;
  video_audio: boolean;
  guidance_scale: number;
  nag_scale: number;
  num_inference_steps: number;
  negative_prompt: string;
  reference_image: string;
  reference_image_url: string;
}

interface KnobsSidebarProps {
  config: RenderConfig;
  imageBackends: Record<string, string>;
  videoBackends: Record<string, string>;
  onSave: (config: RenderConfig) => void;
  onUploadGlobalRef: (file: File) => void;
  onClearGlobalRef: () => void;
  mediaUrl: (path: string) => string;
}

export default function KnobsSidebar({
  config: initialConfig,
  imageBackends,
  videoBackends,
  onSave,
  onUploadGlobalRef,
  onClearGlobalRef,
  mediaUrl
}: KnobsSidebarProps) {
  const [config, setConfig] = useState<RenderConfig>(initialConfig);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    setConfig(initialConfig);
  }, [initialConfig]);

  const handleCheckboxChange = (val: string, checked: boolean) => {
    const selected = config.backend.split(",").map(b => b.trim()).filter(Boolean);
    let updated;
    if (checked) {
      updated = [...selected, val];
    } else {
      updated = selected.filter(v => v !== val);
    }
    setConfig({ ...config, backend: updated.join(",") || "nano2" });
  };

  const handleSave = () => {
    onSave(config);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith("image/")) {
      onUploadGlobalRef(file);
    }
  };

  return (
    <div className="w-80 bg-zinc-950/80 border-l border-zinc-900 flex flex-col h-full shrink-0">
      <div className="p-4 border-b border-zinc-900 flex items-center justify-between">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <Settings className="h-4 w-4 text-amber-500" />
          Generation Knobs
        </h3>
        <button
          onClick={handleSave}
          className="bg-amber-500 hover:bg-amber-600 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1 shadow-md shadow-amber-500/10"
        >
          <Save className="h-3.5 w-3.5" />
          Save
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-5 text-zinc-300">
        {/* Image backends multi-select */}
        <div className="flex flex-col gap-2">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            Default Image Models
          </label>
          <div className="flex flex-col gap-2 bg-zinc-900/40 p-3 rounded-lg border border-zinc-900 shadow-inner">
            {Object.entries(imageBackends).map(([v, label]) => (
              <label key={v} className="flex items-center gap-2.5 text-xs text-zinc-300 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={config.backend.split(",").includes(v)}
                  onChange={(e) => handleCheckboxChange(v, e.target.checked)}
                  className="rounded bg-zinc-950 border-zinc-800 text-amber-500 focus:ring-amber-500/20 h-4 w-4"
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Video model */}
        <div className="flex flex-col gap-1.5">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            Default Video Model
          </label>
          <select
            value={config.video_model}
            onChange={(e) => setConfig({ ...config, video_model: e.target.value })}
            className="w-full bg-zinc-950 text-amber-300 text-xs px-3 py-2 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer font-semibold"
          >
            {Object.entries(videoBackends).map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </div>

        {/* Chaining mode */}
        <div className="flex flex-col gap-1.5">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            Sequence Chaining Mode
          </label>
          <select
            value={config.video_chaining}
            onChange={(e) => setConfig({ ...config, video_chaining: e.target.value })}
            className="w-full bg-zinc-950 text-zinc-300 text-xs px-3 py-2 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer"
          >
            <option value="native_extend">Native Video Extend (Seedance/Luma)</option>
            <option value="opencv_chain">OpenCV Final Frame Chaining</option>
            <option value="independent">Independent Still Drafts</option>
          </select>
        </div>

        {/* Audio Enabled */}
        <div className="flex flex-col gap-1.5">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            🔊 Video Native Audio
          </label>
          <select
            value={config.video_audio ? "true" : "false"}
            onChange={(e) => setConfig({ ...config, video_audio: e.target.value === "true" })}
            className="w-full bg-zinc-950 text-zinc-300 text-xs px-3 py-2 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer"
          >
            <option value="true">Enabled (Sound synthesis)</option>
            <option value="false">Disabled (Silent b-roll)</option>
          </select>
        </div>

        {/* Sliders */}
        <div className="grid grid-cols-2 gap-3 border-t border-zinc-900 pt-4">
          <div className="flex flex-col gap-1">
            <span className="text-[9px] uppercase font-bold text-zinc-500 font-mono">CFG Guidance</span>
            <input
              type="number"
              step="0.1"
              value={config.guidance_scale}
              onChange={(e) => setConfig({ ...config, guidance_scale: parseFloat(e.target.value) || 3.5 })}
              className="bg-zinc-950 text-zinc-200 text-xs px-2.5 py-1.5 rounded border border-zinc-800"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[9px] uppercase font-bold text-zinc-500 font-mono">Neg Strength</span>
            <input
              type="number"
              step="0.1"
              value={config.nag_scale}
              onChange={(e) => setConfig({ ...config, nag_scale: parseFloat(e.target.value) || 5.0 })}
              className="bg-zinc-950 text-zinc-200 text-xs px-2.5 py-1.5 rounded border border-zinc-800"
            />
          </div>
          <div className="flex flex-col gap-1 col-span-2">
            <span className="text-[9px] uppercase font-bold text-zinc-500 font-mono">Inference Steps</span>
            <input
              type="number"
              value={config.num_inference_steps}
              onChange={(e) => setConfig({ ...config, num_inference_steps: parseInt(e.target.value) || 28 })}
              className="bg-zinc-950 text-zinc-200 text-xs px-2.5 py-1.5 rounded border border-zinc-800 w-full"
            />
          </div>
        </div>

        {/* Negative prompt */}
        <div className="flex flex-col gap-1.5 border-t border-zinc-900 pt-4">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            Negative Prompt override
          </label>
          <textarea
            value={config.negative_prompt}
            onChange={(e) => setConfig({ ...config, negative_prompt: e.target.value })}
            placeholder="Blank defaults to cinematic standard exclusions"
            className="w-full bg-zinc-950 text-zinc-300 placeholder-zinc-700 border border-zinc-800 rounded-lg p-2.5 h-16 focus:outline-none focus:border-amber-400 transition text-xs resize-none"
          />
        </div>

        {/* Global Reference Frame */}
        <div className="flex flex-col gap-2 border-t border-zinc-900 pt-4">
          <label className="text-[10px] uppercase font-bold tracking-wider text-zinc-500 font-mono">
            Global Frame Reference
          </label>
          
          {config.reference_image ? (
            <div className="flex items-center gap-2.5 bg-zinc-900/60 p-2 border border-zinc-800 rounded-lg shadow-inner">
              <img
                src={mediaUrl(config.reference_image)}
                className="h-10 w-16 object-cover rounded border border-zinc-800"
                alt="Global ref frame"
              />
              <button
                onClick={onClearGlobalRef}
                className="text-rose-400 hover:text-rose-300 text-[10px] font-semibold px-2 py-1 bg-zinc-950/80 rounded border border-zinc-800 transition hover:bg-zinc-900"
              >
                ✕ remove
              </button>
            </div>
          ) : (
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              onClick={() => document.getElementById("global-ref-file")?.click()}
              className={`border border-dashed rounded-lg p-4 text-center cursor-pointer transition-all duration-200 text-xs flex flex-col items-center gap-1.5 ${
                dragOver ? "border-amber-500 bg-amber-500/5 text-zinc-200" : "border-zinc-800 text-zinc-500 hover:text-zinc-400"
              }`}
            >
              <Upload className="h-4 w-4" />
              <span>Drag &amp; drop or click to set frame reference</span>
              <input
                type="file"
                id="global-ref-file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) onUploadGlobalRef(file);
                }}
              />
            </div>
          )}
          <span className="text-[9px] text-zinc-500 italic">Forces visual layout consistency (Nano Banana 2 only)</span>
        </div>
      </div>
    </div>
  );
}
