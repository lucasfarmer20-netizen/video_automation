"use client";

import React, { useState } from "react";
import { Mic, Sliders, Sparkles, X, Volume2, Check } from "lucide-react";

interface Preview {
  generated_voice_id: string;
  audio_data_uri: string;
  duration_secs?: number;
}

interface VoiceStudioModalProps {
  isOpen: boolean;
  onClose: () => void;
  post: (url: string, body?: any) => Promise<any>;
  mediaUrl?: (path: string) => string;
}

export default function VoiceStudioModal({ isOpen, onClose, post, mediaUrl }: VoiceStudioModalProps) {
  const [gender, setGender] = useState("male");
  const [age, setAge] = useState("middle_aged");
  const [accent, setAccent] = useState("american");
  const [description, setDescription] = useState(
    "A low-pitched raspy documentary investigator with deep vocal fry and slow deliberate cadence"
  );
  
  const [stability, setStability] = useState(0.35);
  const [styleExaggeration, setStyleExaggeration] = useState(0.35);
  
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  // The current API returns three candidates per design, and a preview is NOT a
  // usable voice — its generated_voice_id must be promoted via /api/voice/save
  // before narration can use it.
  const [previews, setPreviews] = useState<Preview[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [voiceName, setVoiceName] = useState("Vesper");
  const [savedVoiceId, setSavedVoiceId] = useState<string | null>(null);
  const [sampleText, setSampleText] = useState("");

  if (!isOpen) return null;

  const handleGenerateVoice = async () => {
    setLoading(true);
    setMessage("");
    setPreviews([]);
    setChosen(null);
    try {
      const res = await post("/api/voice/design", {
        gender,
        age,
        accent,
        description,
        sample_text: sampleText
      });
      if (res.ok && Array.isArray(res.previews) && res.previews.length) {
        setPreviews(res.previews);
        setChosen(res.previews[0].generated_voice_id);
        setMessage(`${res.previews.length} candidates generated. Audition them, then save the one you want.`);
      } else {
        setMessage(`Voice design failed: ${res.error || "no previews returned"}`);
      }
    } catch (e: any) {
      setMessage(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  // Promote the chosen audition into a real voice and assign it to this episode.
  // Skipping this loses the voice: previews are throwaway.
  const handleSaveVoice = async () => {
    if (!chosen) return;
    setLoading(true);
    try {
      const res = await post("/api/voice/save", {
        generated_voice_id: chosen,
        name: voiceName,
        voice_description: description
      });
      if (res.ok && res.voice_id) {
        setSavedVoiceId(res.voice_id);
        setMessage(`Saved "${res.name}" (${res.voice_id}) and set it as this episode's narrator.`);
      } else {
        setMessage(`Save failed: ${res.error || "no voice_id returned"}`);
      }
    } catch (e: any) {
      setMessage(`Save failed: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSaveSettings = async () => {
    setLoading(true);
    try {
      // Only ever send a real, saved voice_id. This used to send a preview's
      // generated_voice_id, which is not a usable voice.
      const payload: any = { stability, style_exaggeration: styleExaggeration };
      if (savedVoiceId) payload.voice_id = savedVoiceId;
      
      const res = await post("/api/voice/settings", payload);
      if (res.ok) {
        setMessage("ElevenLabs Voice parameters updated!");
        setTimeout(() => onClose(), 1000);
      }
    } catch (e: any) {
      setMessage(`Save failed: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur-md flex items-center justify-center p-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl w-full max-w-xl shadow-2xl overflow-hidden text-zinc-100 flex flex-col max-h-[90vh]">
        
        {/* Header */}
        <div className="px-6 py-4 border-b border-zinc-800 flex items-center justify-between bg-zinc-950/40">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-500">
              <Mic className="w-4 h-4" />
            </div>
            <div>
              <h3 className="text-sm font-bold tracking-wide">ElevenLabs AI Voice Studio</h3>
              <p className="text-[11px] text-zinc-400">Design 100% unique documentary voices & tune vocal cadence</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto space-y-6 flex-1">
          
          {/* AI Voice Designer Section */}
          <div className="bg-zinc-950/60 p-4 rounded-xl border border-zinc-800 space-y-4">
            <div className="flex items-center gap-2 text-amber-400 text-xs font-bold font-mono uppercase tracking-wider">
              <Sparkles className="w-3.5 h-3.5" />
              <span>Voice Design Generator (Zero Uploads)</span>
            </div>

            <div className="grid grid-cols-3 gap-3 text-xs">
              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Gender</label>
                <select
                  value={gender}
                  onChange={(e) => setGender(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="male">Male</option>
                  <option value="female">Female</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Age</label>
                <select
                  value={age}
                  onChange={(e) => setAge(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="middle_aged">Middle Aged</option>
                  <option value="old">Old</option>
                  <option value="young">Young</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Accent</label>
                <select
                  value={accent}
                  onChange={(e) => setAccent(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="american">American</option>
                  <option value="british">British</option>
                  <option value="irish">Irish</option>
                  <option value="scottish">Scottish</option>
                  <option value="australian">Australian</option>
                </select>
              </div>
            </div>

            <div>
              <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Voice Description Prompt</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
                placeholder="Describe tone, cadence, raspy pitch, vocal fry..."
              />
            </div>

            <div>
              <label className="block text-[10px] text-zinc-400 mb-1 font-mono">
                Audition line (optional — leave blank to auto-generate)
              </label>
              <textarea
                value={sampleText}
                onChange={(e) => setSampleText(e.target.value)}
                rows={2}
                className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
                placeholder="Paste a real line of Vesper narration to hear the voice on your own copy..."
              />
            </div>

            <button
              onClick={handleGenerateVoice}
              disabled={loading}
              className="w-full py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 font-bold text-xs rounded-lg shadow-md transition flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>{loading ? "Generating Unique Voice..." : "Generate Unique AI Voice"}</span>
            </button>

            {previews.length > 0 && (
              <div className="space-y-2 mt-3">
                <div className="text-[10px] text-zinc-400 font-mono uppercase tracking-wider">
                  Audition candidates — pick one, name it, then save
                </div>
                {previews.map((pv, i) => (
                  <label
                    key={pv.generated_voice_id}
                    className={`block rounded-xl p-3 border cursor-pointer transition ${
                      chosen === pv.generated_voice_id
                        ? "bg-amber-500/10 border-amber-500/40"
                        : "bg-zinc-900 border-zinc-800 hover:border-zinc-700"
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <input
                        type="radio"
                        name="voice-preview"
                        checked={chosen === pv.generated_voice_id}
                        onChange={() => setChosen(pv.generated_voice_id)}
                        className="accent-amber-500"
                      />
                      <span className="text-xs font-bold text-zinc-200">Candidate {i + 1}</span>
                      {pv.duration_secs && (
                        <span className="text-[10px] text-zinc-500 font-mono">
                          {pv.duration_secs.toFixed(1)}s
                        </span>
                      )}
                    </div>
                    <audio
                      controls
                      src={pv.audio_data_uri}
                      className="w-full h-8 rounded border border-zinc-800 bg-zinc-950 accent-amber-500"
                    />
                  </label>
                ))}

                <div className="flex gap-2 pt-1">
                  <input
                    type="text"
                    value={voiceName}
                    onChange={(e) => setVoiceName(e.target.value)}
                    placeholder="Voice name"
                    className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
                  />
                  <button
                    onClick={handleSaveVoice}
                    disabled={loading || !chosen || !voiceName.trim()}
                    className="px-4 py-2 rounded-lg text-xs font-bold bg-emerald-500 hover:bg-emerald-400 text-zinc-950 transition disabled:opacity-50 whitespace-nowrap"
                  >
                    Save &amp; use for this episode
                  </button>
                </div>
                {savedVoiceId && (
                  <div className="text-[10px] text-emerald-400 font-mono">
                    Active narrator: {savedVoiceId}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Voice Tuning Sliders Section */}
          <div className="bg-zinc-950/60 p-4 rounded-xl border border-zinc-800 space-y-4">
            <div className="flex items-center gap-2 text-zinc-300 text-xs font-bold font-mono uppercase tracking-wider">
              <Sliders className="w-3.5 h-3.5 text-amber-500" />
              <span>Vocal Cadence & Resonance Tuning</span>
            </div>

            {/* Stability Slider */}
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-zinc-400 font-mono">Stability (Vocal Variation)</span>
                <span className="text-amber-400 font-mono font-bold">{stability.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min="0.1"
                max="1.0"
                step="0.05"
                value={stability}
                onChange={(e) => setStability(parseFloat(e.target.value))}
                className="w-full accent-amber-500"
              />
              <p className="text-[10px] text-zinc-500">Lower stability (0.30 - 0.40) introduces emotional pitch drops and dramatic human breathing.</p>
            </div>

            {/* Style Exaggeration Slider */}
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-zinc-400 font-mono">Style Exaggeration</span>
                <span className="text-amber-400 font-mono font-bold">{styleExaggeration.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min="0.0"
                max="1.0"
                step="0.05"
                value={styleExaggeration}
                onChange={(e) => setStyleExaggeration(parseFloat(e.target.value))}
                className="w-full accent-amber-500"
              />
              <p className="text-[10px] text-zinc-500">Higher style exaggeration amplifies heavy documentary delivery and vocal weight.</p>
            </div>
          </div>

          {message && (
            <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs font-mono">
              {message}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-zinc-800 bg-zinc-950/40 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-xs font-semibold bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition"
          >
            Cancel
          </button>
          <button
            onClick={handleSaveSettings}
            disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-bold bg-emerald-500 hover:bg-emerald-400 text-zinc-950 shadow-md transition flex items-center gap-1.5 disabled:opacity-50"
          >
            <Check className="w-4 h-4" />
            <span>Apply Voice Parameters</span>
          </button>
        </div>

      </div>
    </div>
  );
}
