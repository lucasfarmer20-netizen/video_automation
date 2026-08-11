"use client";

import React, { useState, useEffect, useRef } from "react";
import { Copy, Film, Image as ImageIcon, Plus, Trash2, Send, Sparkles, Check, Play, Edit3 } from "lucide-react";

interface Camera {
  move: string;
  duration: number;
  speed: number;
  duration_locked?: boolean;
}

interface ShotRef {
  name: string;
  file: string | null;
}

interface Shot {
  scene_id: string;
  narration: string;
  prompt: string;
  style_medium: string;
  motion_prompt: string;
  chosen_variation: number | null;
  chosen_video_variation?: number | null;
  motion_type: string;
  camera: Camera;
  fx: string[];
  sfx: string;
  references: string[];
  video_model: string | null;
  video_audio: boolean | null;
  flow_hero: boolean;
  hero_clip: boolean;
  draft_variations: string[];
  draft_image: string | null;
  video_variations: string[];
  video_clip: string | null;
  approved: boolean;
  notes: string;
  references_resolved?: ShotRef[];
  motion_prompt_suggestion?: string;
  active_clip_url?: string | null;
}

interface Message {
  role: "user" | "assistant";
  content: string;
}

interface BeatCardProps {
  shot: Shot;
  videoBackends: Record<string, string>;
  tiers: Record<string, string>;
  onUpdateField: (sceneId: string, field: string, value: any) => void;
  onRegenStill: (sceneId: string, btn: HTMLButtonElement) => void;
  onGenerateVideo: (sceneId: string, btn: HTMLButtonElement) => void;
  onUploadImage: (sceneId: string, file: File) => void;
  onUploadClip: (sceneId: string, file: File) => void;
  onAddReference: (sceneId: string, file: File) => void;
  onRemoveReference: (sceneId: string, name: string) => void;
  onDeleteImage: (sceneId: string, idx: number) => void;
  onEditImage: (sceneId: string, idx: number, promptText: string) => void;
  onDeleteVideo: (sceneId: string, idx: number) => void;
  onSelectVariation: (sceneId: string, idx: number) => void;
  /** Opens the full-size take viewer at this variation. */
  onOpenImage?: (sceneId: string, images: string[], idx: number, chosen: number | null) => void;
  onSelectVideoVariation: (sceneId: string, idx: number) => void;
  onSendShotChat: (sceneId: string, text: string, chatHistory: Message[]) => Promise<{ reply: string, refined_prompt?: string, refined_motion_prompt?: string } | null>;
  onApplyRefinedPrompts: (sceneId: string, refinedPrompt: string | null, refinedMotionPrompt: string | null) => void;
  onOpenDirectorWorkspace?: (sceneId: string) => void;
  mediaUrl: (path: string) => string;
}

export default function BeatCard({
  shot,
  videoBackends = {},
  tiers = {},
  onUpdateField,
  onRegenStill,
  onGenerateVideo,
  onUploadImage,
  onUploadClip,
  onAddReference,
  onRemoveReference,
  onDeleteImage,
  onEditImage,
  onDeleteVideo,
  onSelectVariation,
  onOpenImage,
  onSelectVideoVariation,
  onSendShotChat,
  onApplyRefinedPrompts,
  onOpenDirectorWorkspace,
  mediaUrl
}: BeatCardProps) {
  const [shotChatHistory, setShotChatHistory] = useState<Message[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [refinedPrompts, setRefinedPrompts] = useState<{ prompt: string | null, motion_prompt: string | null } | null>(null);
  
  const regenBtnRef = useRef<HTMLButtonElement>(null);
  const videoBtnRef = useRef<HTMLButtonElement>(null);

  const [dragRefOver, setDragRefOver] = useState(false);
  const [dragImgOver, setDragImgOver] = useState(false);
  const [dragClipOver, setDragClipOver] = useState(false);

  const handleCopyPrompt = () => {
    navigator.clipboard.writeText(shot.motion_prompt || shot.motion_prompt_suggestion || "");
    alert("Motion prompt copied to clipboard!");
  };

  const handleSendChat = async () => {
    const text = chatInput.trim();
    if (!text || chatLoading) return;
    setChatInput("");
    setChatLoading(true);

    const newHistory = [...shotChatHistory, { role: "user" as const, content: text }];
    setShotChatHistory(newHistory);

    const res = await onSendShotChat(shot.scene_id, text, newHistory);
    setChatLoading(false);

    if (res) {
      setShotChatHistory([...newHistory, { role: "assistant" as const, content: res.reply }]);
      if (res.refined_prompt || res.refined_motion_prompt) {
        setRefinedPrompts({
          prompt: res.refined_prompt || null,
          motion_prompt: res.refined_motion_prompt || null
        });
      }
    }
  };

  return (
    <div
      className={`rounded-2xl p-5 border relative transition-all duration-300 backdrop-blur-md shadow-xl ${
        shot.approved ? "border-emerald-500/40 bg-emerald-950/20 shadow-[0_0_15px_rgba(52,211,153,0.1)]" : ""
      } ${shot.flow_hero ? "border-amber-400/50 bg-amber-950/20 shadow-[0_0_15px_rgba(245,158,11,0.1)]" : ""} ${
        shot.motion_type === "ai_video" ? "border-purple-500/30 bg-purple-950/20 shadow-[0_0_15px_rgba(192,132,252,0.1)]" : "border-zinc-800/80 bg-zinc-950/85 hover:border-zinc-700 hover:shadow-2xl"
      }`}
      id={`beat-${shot.scene_id}`}
    >
      {/* Connective Timeline element */}
      <div className="absolute left-[31px] top-[50px] bottom-[-40px] w-0.5 bg-zinc-900 pointer-events-none group-last:hidden"></div>

      <div className="flex flex-col md:flex-row gap-5 items-start">
        {/* Left Side Metadata */}
        <div className="flex md:flex-col items-center justify-between md:justify-start gap-2 min-w-[70px] w-full md:w-auto shrink-0">
          <span className="text-amber-500 font-mono text-xs font-bold bg-zinc-950 border border-zinc-900 px-2.5 py-1 rounded shadow-inner">
            {shot.scene_id}
          </span>
          <div className="flex items-center gap-1 text-[10px] font-mono">
            <input
              type="number" step={0.1} min={0.2}
              defaultValue={shot.camera.duration}
              key={`d-${shot.scene_id}-${shot.camera.duration}`}
              onBlur={(e) => {
                const v = parseFloat(e.target.value);
                if (Number.isFinite(v) && Math.abs(v - shot.camera.duration) > 0.05)
                  onUpdateField(shot.scene_id, "camera", { duration: v });
              }}
              title="Target length for this beat, in seconds"
              className="w-14 bg-zinc-950 text-amber-400 border border-zinc-800 rounded px-1 py-0.5 text-right focus:outline-none focus:border-amber-400"
            />
            <span className="text-zinc-600">s</span>
            <button
              onClick={() => onUpdateField(shot.scene_id, "camera",
                { duration_locked: !shot.camera.duration_locked })}
              title={shot.camera.duration_locked
                ? "Locked — re-running narration will not retime this beat"
                : "Unlocked — re-running narration refits this beat to its voiceover"}
              className={`transition ${shot.camera.duration_locked
                ? "text-amber-500" : "text-zinc-600 hover:text-zinc-400"}`}
            >
              {shot.camera.duration_locked ? "🔒" : "🔓"}
            </button>
          </div>
          {onOpenDirectorWorkspace && (
            <button
              onClick={() => onOpenDirectorWorkspace(shot.scene_id)}
              className="mt-2 px-2.5 py-1 text-[10px] font-mono font-bold text-amber-400 bg-amber-500/15 border border-amber-500/30 hover:bg-amber-500/25 rounded-md transition-colors flex items-center gap-1 shadow"
              title="Open Director Workspace for this scene"
            >
              <Sparkles className="w-3 h-3 text-amber-400" />
              <span>DIRECT</span>
            </button>
          )}
        </div>

        {/* Center Inputs Column */}
        <div className="flex-1 w-full flex flex-col gap-4">
          <div>
            <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
              Narration (Voiceover)
            </label>
            <textarea
              rows={5}
              // Same reason as the prompts: a controlled textarea wrote to the
              // manifest on every keystroke, and narration is the longest field
              // on the card.
              defaultValue={shot.narration}
              key={`n-${shot.scene_id}-${shot.narration}`}
              onBlur={(e) => {
                if (e.target.value !== shot.narration)
                  onUpdateField(shot.scene_id, "narration", e.target.value);
              }}
              className="w-full bg-zinc-950/60 text-zinc-200 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition text-xs font-sans leading-relaxed resize-y"
            />
          </div>

          <div>
            <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
              Visual Scene Prompt
            </label>
            <textarea
              rows={6}
              // defaultValue + onBlur: a controlled textarea posted to
              // /api/shot/{id} on every keystroke, and each write is a GCS round
              // trip. The key re-seeds the box when the prompt changes elsewhere.
              defaultValue={shot.prompt}
              key={`p-${shot.scene_id}-${shot.prompt}`}
              onBlur={(e) => {
                if (e.target.value !== shot.prompt)
                  onUpdateField(shot.scene_id, "prompt", e.target.value);
              }}
              className="w-full bg-zinc-950/60 text-zinc-200 border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition text-xs font-sans leading-relaxed resize-y min-h-[7rem]"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
                Style Medium
              </label>
              <input
                type="text"
                defaultValue={shot.style_medium}
                key={`s-${shot.scene_id}-${shot.style_medium}`}
                onBlur={(e) => {
                  if (e.target.value !== shot.style_medium)
                    onUpdateField(shot.scene_id, "style_medium", e.target.value);
                }}
                className="w-full bg-zinc-950/60 text-zinc-200 border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition text-xs"
              />
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1 gap-2">
              <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider font-mono">
                🎬 Video Motion Prompt
              </label>
              {shot.motion_prompt_suggestion && (
                <button
                  onClick={() => onUpdateField(shot.scene_id, "motion_prompt", shot.motion_prompt_suggestion)}
                  className="text-[10px] font-mono text-zinc-500 hover:text-amber-400 transition"
                  title="Copy the suggestion into the field"
                >
                  use suggestion
                </button>
              )}
              {shot.motion_prompt && (
                <button
                  onClick={() => onUpdateField(shot.scene_id, "motion_prompt", "")}
                  className="text-[10px] font-mono text-zinc-600 hover:text-red-400 transition"
                  title="Clear this prompt"
                >
                  clear
                </button>
              )}
            </div>
            <textarea
              rows={5}
              // The suggestion is a PLACEHOLDER, never the value. It used to fall
              // back into `value`, so an empty prompt displayed the suggestion as
              // real text: clearing the box instantly refilled it, and typing
              // saved your words merged into the suggestion that was sitting
              // there. That is the "stacking".
              defaultValue={shot.motion_prompt || ""}
              key={`m-${shot.scene_id}-${shot.motion_prompt}`}
              placeholder={shot.motion_prompt_suggestion || "Describe the motion for this beat…"}
              onBlur={(e) => {
                if (e.target.value !== (shot.motion_prompt || ""))
                  onUpdateField(shot.scene_id, "motion_prompt", e.target.value);
              }}
              className="w-full bg-zinc-950/60 text-zinc-200 placeholder:text-zinc-600 placeholder:italic border border-zinc-800 rounded-lg px-3 py-2 focus:outline-none focus:border-amber-400 transition text-xs font-sans leading-relaxed resize-y min-h-[6rem]"
            />
            <p className="text-[9px] text-zinc-600 mt-1">
              Empty uses the greyed suggestion. Only what you type here is saved.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3 mt-1">
            <button
              onClick={handleCopyPrompt}
              className="bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 px-2.5 py-1 rounded text-[10px] font-semibold transition"
            >
              Copy Motion Prompt
            </button>
            <div
              onDragOver={(e) => { e.preventDefault(); setDragClipOver(true); }}
              onDragLeave={() => setDragClipOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragClipOver(false);
                const file = e.dataTransfer.files[0];
                if (file && file.type.startsWith("video/")) onUploadClip(shot.scene_id, file);
              }}
              onClick={() => document.getElementById(`clipfile-${shot.scene_id}`)?.click()}
              className={`border border-dashed rounded-lg px-2.5 py-1 text-[10px] cursor-pointer transition flex items-center gap-1.5 ${
                dragClipOver ? "border-amber-500 bg-amber-500/5 text-zinc-200" : "border-zinc-800 text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <Film className="h-3 w-3" />
              <span>Import hero clip (Veo/Flow)</span>
              <input
                type="file"
                id={`clipfile-${shot.scene_id}`}
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) onUploadClip(shot.scene_id, file);
                }}
              />
            </div>
            <span className="text-[10px] text-zinc-500 font-mono">
              {shot.hero_clip ? "✓ hero clip imported" : `target ~${Math.round(shot.camera.duration)}s`}
            </span>
          </div>

          {/* Shot Chat Refiner */}
          <div className="mt-3 bg-zinc-950/80 border border-zinc-900 rounded-lg p-3 shadow-inner">
            <div className="flex items-center justify-between mb-2 border-b border-zinc-900 pb-1.5">
              <span className="text-[10px] font-bold text-amber-500 font-mono flex items-center gap-1.5 select-none">
                💬 Vesper Beat Refiner
              </span>
              {refinedPrompts && (
                <button
                  onClick={() => {
                    onApplyRefinedPrompts(shot.scene_id, refinedPrompts.prompt, refinedPrompts.motion_prompt);
                    setRefinedPrompts(null);
                  }}
                  className="bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 text-[9px] px-2.5 py-0.5 rounded font-bold border border-emerald-500/30 transition shadow flex items-center gap-1"
                >
                  <Sparkles className="h-3 w-3" />
                  <span>Apply Vesper's Refinement</span>
                </button>
              )}
            </div>
            
            <div className="max-h-24 overflow-y-auto flex flex-col gap-2 mb-2 text-[10px] bg-zinc-950 p-2.5 rounded border border-zinc-900/60 font-mono">
              {shotChatHistory.length === 0 ? (
                <div className="text-zinc-600 italic">Discuss updates to this specific beat's prompts with Vesper.</div>
              ) : (
                shotChatHistory.map((m, idx) => (
                  <div key={idx} className={m.role === "user" ? "text-zinc-300" : "text-amber-300"}>
                    <span className="font-bold">{m.role === "user" ? "You: " : "Vesper: "}</span>
                    <span>{m.content}</span>
                  </div>
                ))
              )}
            </div>
            
            <div className="flex gap-2">
              <input
                type="text"
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSendChat()}
                placeholder="e.g. 'make the woods darker, add thick fog, slow down camera pan'"
                className="flex-1 bg-zinc-950 text-zinc-200 text-xs border border-zinc-800 rounded-lg px-3 py-1.5 focus:outline-none focus:border-amber-400 transition"
              />
              <button
                onClick={handleSendChat}
                disabled={chatLoading || !chatInput.trim()}
                className="bg-amber-500 hover:bg-amber-600 text-zinc-950 text-[11px] font-bold px-3 py-1.5 rounded-lg transition"
              >
                Send
              </button>
            </div>
          </div>
        </div>

        {/* Right Controls Column */}
        <div className="w-full md:w-44 flex flex-col gap-3 border-t md:border-t-0 md:border-l border-zinc-900 pt-3 md:pt-0 md:pl-4 shrink-0">
          <div>
            <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
              Motion Tier
            </label>
            <select
              value={shot.motion_type}
              onChange={(e) => onUpdateField(shot.scene_id, "motion_type", e.target.value)}
              className="w-full bg-zinc-950 text-zinc-300 text-xs px-2.5 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer font-medium"
            >
              {Object.entries(tiers).map(([k, label]) => (
                <option key={k} value={k}>
                  {label.split(":")[0]}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-amber-500/80 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
              Video Model
            </label>
            <select
              value={shot.video_model || ""}
              onChange={(e) => onUpdateField(shot.scene_id, "video_model", e.target.value)}
              className="w-full bg-zinc-950 text-amber-300/90 text-xs px-2.5 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer"
            >
              <option value="">Use Project default</option>
              {Object.entries(videoBackends).map(([k, label]) => (
                <option key={k} value={k}>
                  {label.split(" ")[0]}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-zinc-500 text-[10px] font-semibold uppercase tracking-wider mb-1 font-mono">
              🔊 Video Audio
            </label>
            <select
              value={shot.video_audio === null ? "none" : (shot.video_audio ? "true" : "false")}
              onChange={(e) => {
                const val = e.target.value;
                onUpdateField(shot.scene_id, "video_audio", val === "none" ? null : val === "true");
              }}
              className="w-full bg-zinc-950 text-zinc-300 text-xs px-2.5 py-1.5 rounded-lg border border-zinc-800 focus:outline-none focus:border-amber-400 transition cursor-pointer font-medium"
            >
              <option value="none">Use Global Knob</option>
              <option value="true">Enabled (Sound)</option>
              <option value="false">Disabled (Silent)</option>
            </select>
          </div>

          <label className="flex items-center gap-2 text-zinc-400 text-xs font-semibold cursor-pointer hover:text-zinc-200 select-none mt-1">
            <input
              type="checkbox"
              checked={shot.flow_hero}
              onChange={(e) => onUpdateField(shot.scene_id, "flow_hero", e.target.checked)}
              className="rounded bg-zinc-950 border-zinc-800 text-amber-500 focus:ring-amber-500/20"
            />
            <span>VEO/Flow hero</span>
          </label>
        </div>
      </div>

      {/* Image Variations Grid */}
      <div className="mt-5 pt-4 border-t border-zinc-900/60">
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-zinc-500 text-[10px] font-semibold uppercase tracking-wider font-mono">
            Draft Variations {shot.draft_variations && shot.draft_variations.length > 0 && `(${shot.draft_variations.length})`}
          </h4>
          {shot.draft_variations && shot.draft_variations.length > 0 && (
            <span className="text-[9px] text-zinc-600 font-mono select-none">Click a tile to choose active frame</span>
          )}
        </div>
        {shot.draft_variations && shot.draft_variations.length > 0 ? (
          <div className="grid grid-cols-3 gap-4">
            {shot.draft_variations.map((path, idx) => (
              <div
                key={idx}
                onClick={() => onOpenImage
                  ? onOpenImage(shot.scene_id, shot.draft_variations, idx, shot.chosen_variation)
                  : onSelectVariation(shot.scene_id, idx)}
                title="Click to view full size"
                className={`group relative rounded-lg overflow-hidden cursor-zoom-in bg-zinc-950 aspect-video border-2 transition-all duration-200 ${
                  shot.chosen_variation === idx
                    ? "border-amber-500 shadow-[0_0_15px_rgba(245,158,11,0.08)]"
                    : "border-transparent hover:border-zinc-800"
                }`}
              >
                <img src={mediaUrl(path)} loading="lazy" className="w-full h-full object-cover" alt="Draft Variation" />
                <div className="absolute top-2 left-2 bg-zinc-950/80 backdrop-blur-xs text-zinc-300 text-[9px] font-mono px-1.5 py-0.5 rounded border border-zinc-800/80 select-none">
                  #{idx + 1}
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); onSelectVariation(shot.scene_id, idx); }}
                  title={shot.chosen_variation === idx ? "This take is in use" : "Use this take"}
                  className={`absolute top-2 right-2 bg-amber-500 text-zinc-950 rounded-full w-5 h-5 flex items-center justify-center text-[10px] font-extrabold shadow-lg transition-all hover:scale-110 ${
                    shot.chosen_variation === idx ? "opacity-100 scale-100" : "opacity-0 scale-90 group-hover:opacity-100"
                  }`}
                >
                  <Check className="h-3 w-3" strokeWidth={3} />
                </button>
                {/* Delete still */}
                <button
                  onClick={(e) => { e.stopPropagation(); if (confirm("Delete this variation?")) onDeleteImage(shot.scene_id, idx); }}
                  className="absolute bottom-2 right-2 bg-rose-600/85 hover:bg-rose-600 text-white rounded p-1.5 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                  title="Delete variation"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
                {/* Edit i2i still */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    const promptText = prompt("Describe modifications for image-to-image refinement:");
                    if (promptText) onEditImage(shot.scene_id, idx, promptText);
                  }}
                  className="absolute bottom-2 left-2 bg-amber-500 hover:bg-amber-600 text-zinc-950 rounded p-1.5 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                  title="Refine image (Image-to-Image)"
                >
                  <Edit3 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-zinc-600 italic py-2">No draft stills generated. Click "Regenerate Still" below to generate.</div>
        )}
      </div>

      {/* Video Variations Grid */}
      <div className="mt-5 pt-4 border-t border-zinc-900/60">
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-zinc-500 text-[10px] font-semibold uppercase tracking-wider font-mono">
            🎬 Video Renders {shot.video_variations && shot.video_variations.length > 0 && `(${shot.video_variations.length})`}
          </h4>
          {shot.video_variations && shot.video_variations.length > 0 && (
            <span className="text-[9px] text-zinc-600 font-mono select-none">Hover to preview · Click to set timeline clip</span>
          )}
        </div>
        {shot.video_variations && shot.video_variations.length > 0 ? (
          <div className="grid grid-cols-3 gap-4">
            {shot.video_variations.map((vpath, idx) => (
              <div
                key={idx}
                onClick={() => onSelectVideoVariation(shot.scene_id, idx)}
                className={`group relative rounded-lg overflow-hidden cursor-pointer bg-zinc-950 aspect-video border-2 transition-all duration-200 ${
                  shot.video_clip === vpath
                    ? "border-amber-500 shadow-[0_0_15px_rgba(245,158,11,0.08)]"
                    : "border-transparent hover:border-zinc-800"
                }`}
              >
                <video
                  src={mediaUrl(vpath)}
                  muted
                  loop
                  className="w-full h-full object-cover"
                  onMouseEnter={(e) => e.currentTarget.play()}
                  onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
                  preload="none"
                />
                <div className="absolute top-2 left-2 bg-zinc-950/80 backdrop-blur-xs text-zinc-300 text-[9px] font-mono px-1.5 py-0.5 rounded border border-zinc-800/80 select-none">
                  #{idx + 1}
                </div>
                <div
                  className={`absolute top-2 right-2 bg-amber-500 text-zinc-950 rounded-full w-5 h-5 flex items-center justify-center text-[10px] font-extrabold shadow-lg transition-all ${
                    shot.video_clip === vpath ? "opacity-100 scale-100" : "opacity-0 scale-90 group-hover:opacity-30"
                  }`}
                >
                  <Check className="h-3 w-3" strokeWidth={3} />
                </div>
                {/* Delete video */}
                <button
                  onClick={(e) => { e.stopPropagation(); if (confirm("Delete this video clip?")) onDeleteVideo(shot.scene_id, idx); }}
                  className="absolute bottom-2 right-2 bg-rose-600/85 hover:bg-rose-600 text-white rounded p-1.5 text-xs shadow-lg transition opacity-0 group-hover:opacity-100 flex items-center justify-center hover:scale-105"
                  title="Delete video"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-zinc-600 italic py-1">No video renders generated.</div>
        )}
      </div>

      {/* Active Clip Playout */}
      {shot.active_clip_url && (
        <div className="mt-4 bg-zinc-950/40 p-2 border border-zinc-900 rounded-lg">
          <video
            controls
            playsInline
            preload="none"
            poster={shot.draft_image ? mediaUrl(shot.draft_image) : undefined}
            className="w-full max-h-56 bg-black rounded border border-zinc-900"
            src={mediaUrl(shot.active_clip_url)}
          />
          <div className="text-[10px] text-zinc-500 mt-1.5 font-mono flex items-center justify-between select-none">
            <span>▶ Playout: {shot.hero_clip ? "imported hero clip" : shot.motion_type}</span>
          </div>
        </div>
      )}

      {/* References Row */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragRefOver(true); }}
        onDragLeave={() => setDragRefOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragRefOver(false);
          const file = e.dataTransfer.files[0];
          if (file && file.type.startsWith("image/")) onAddReference(shot.scene_id, file);
        }}
        className="mt-4 flex flex-wrap items-center gap-2.5 border-t border-zinc-900/60 pt-4"
      >
        <span className="text-zinc-500 text-[10px] font-semibold uppercase tracking-wider font-mono">
          References:
        </span>
        {shot.references_resolved?.map((r, idx) => (
          <div key={idx} className="relative w-14 h-9 border border-zinc-800 rounded overflow-hidden group bg-zinc-950" title={r.name}>
            {r.file ? (
              <img src={mediaUrl(r.file)} className="w-full h-full object-cover" alt="ref thumbnail" />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-[8px] text-zinc-500 p-1 text-center leading-tight truncate">
                {r.name}
              </div>
            )}
            <span
              onClick={() => onRemoveReference(shot.scene_id, r.name)}
              className="absolute top-0 right-0 w-3.5 h-3.5 leading-none text-center text-[8px] cursor-pointer bg-zinc-950/80 text-amber-500 hover:bg-rose-600 hover:text-white rounded-bl border-b border-l border-zinc-855 flex items-center justify-center opacity-0 group-hover:opacity-100 transition"
              title="remove reference"
            >
              ✕
            </span>
          </div>
        ))}
        <div
          onClick={() => document.getElementById(`ref-file-${shot.scene_id}`)?.click()}
          className={`border border-dashed rounded-lg px-2.5 py-1 text-[10px] cursor-pointer transition ${
            dragRefOver ? "border-amber-500 bg-amber-500/5 text-zinc-200" : "border-zinc-800 text-zinc-400 hover:text-zinc-200"
          }`}
        >
          + Add Ref
          <input
            type="file"
            id={`ref-file-${shot.scene_id}`}
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onAddReference(shot.scene_id, file);
            }}
          />
        </div>
      </div>

      {/* Action Buttons */}
      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-zinc-900/60 pt-4">
        <button
          ref={regenBtnRef}
          onClick={(e) => onRegenStill(shot.scene_id, e.currentTarget)}
          className="btn-premium px-3.5 py-1.5 rounded-lg text-[10px] font-bold"
        >
          ↻ Regen Still
        </button>
        <button
          ref={videoBtnRef}
          onClick={(e) => onGenerateVideo(shot.scene_id, e.currentTarget)}
          className="btn-accent px-3.5 py-1.5 rounded-lg text-[10px] font-bold flex items-center gap-1 shadow"
        >
          <Play className="h-3 w-3 fill-current" />
          <span>Generate Video</span>
        </button>
        
        {/* Upload Custom Still drop-zone */}
        <div
          onDragOver={(e) => { e.preventDefault(); setDragImgOver(true); }}
          onDragLeave={() => setDragImgOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragImgOver(false);
            const file = e.dataTransfer.files[0];
            if (file && file.type.startsWith("image/")) onUploadImage(shot.scene_id, file);
          }}
          onClick={() => document.getElementById(`imgfile-${shot.scene_id}`)?.click()}
          className={`drop-zone border border-dashed rounded-lg px-3 py-1.5 text-[10px] cursor-pointer transition ${
            dragImgOver ? "border-amber-500 bg-amber-500/5 text-zinc-200" : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Upload Custom Still
          <input
            type="file"
            id={`imgfile-${shot.scene_id}`}
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onUploadImage(shot.scene_id, file);
            }}
          />
        </div>
      </div>
    </div>
  );
}
