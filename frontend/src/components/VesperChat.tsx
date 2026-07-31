"use client";

import React, { useState, useRef, useEffect } from "react";
import { MessageSquare, Send, Sparkles, FileText, Lock } from "lucide-react";

interface Message {
  role: "user" | "assistant";
  content: string;
}

interface VesperChatProps {
  channel: string;
  onDraftStoryboard: (topic: string, beats: number | null) => void;
  onScriptFromChat: (messages: Message[], beats: number | null) => void;
  onLockScript: () => void;
  chatHistory: Message[];
  onSendChatMessage: (text: string) => Promise<string | null>;
  scriptLocked: boolean;
}

export default function VesperChat({
  channel,
  onDraftStoryboard,
  onScriptFromChat,
  onLockScript,
  chatHistory,
  onSendChatMessage,
  scriptLocked
}: VesperChatProps) {
  const [inputText, setInputText] = useState("");
  const [topicText, setTopicText] = useState("");
  const [beatsCount, setBeatsCount] = useState<number | "">("");
  const [loading, setLoading] = useState(false);
  const chatLogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (chatLogRef.current) {
      chatLogRef.current.scrollTop = chatLogRef.current.scrollHeight;
    }
  }, [chatHistory]);

  const handleSend = async () => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText("");
    setLoading(true);
    await onSendChatMessage(text);
    setLoading(false);
  };

  const handleDraftClick = () => {
    if (!topicText.trim()) {
      alert("Enter a topic first");
      return;
    }
    if (confirm("⚠️ DESTRUCTIVE: A fresh AI draft replaces EVERYTHING in this manifest. This cannot be undone. Continue?")) {
      onDraftStoryboard(topicText.trim(), beatsCount === "" ? null : Number(beatsCount));
    }
  };

  const handleScriptFromChatClick = () => {
    if (chatHistory.length === 0) {
      alert("Chat with Vesper first");
      return;
    }
    if (confirm("⚠️ DESTRUCTIVE: This will turn your conversation into a NEW storyboard, OVERWRITING the active project. Continue?")) {
      onScriptFromChat(chatHistory, beatsCount === "" ? null : Number(beatsCount));
    }
  };

  return (
    <aside className="w-80 bg-zinc-950/80 border-l border-zinc-900 flex flex-col h-full shrink-0">
      {/* Header */}
      <div className="p-4 border-b border-zinc-900 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-amber-500 text-sm font-bold flex items-center justify-center h-6 w-6 rounded-full bg-amber-500/10 border border-amber-500/20">
            V
          </span>
          <h3 className="text-amber-400 font-bold text-sm">Develop with Vesper</h3>
        </div>
        <span className="text-[9px] font-mono text-zinc-500 bg-zinc-900/50 px-2 py-0.5 rounded border border-zinc-800 capitalize">
          {channel}
        </span>
      </div>

      {/* Message log */}
      <div
        ref={chatLogRef}
        className="flex-1 overflow-y-auto p-4 flex flex-col gap-3.5 bg-zinc-950/50"
      >
        {chatHistory.length === 0 ? (
          <div className="text-zinc-600 text-xs italic text-center py-20 px-4 leading-relaxed font-mono">
            Start typing below to converse with Vesper. Develop storyboard themes, style directions, or research entities.
          </div>
        ) : (
          chatHistory.map((m, idx) => (
            <div
              key={idx}
              className={`p-3 rounded-xl max-w-[85%] flex flex-col gap-1 ${
                m.role === "user"
                  ? "self-end bg-zinc-900 text-zinc-100 border border-zinc-800/80"
                  : "self-start bg-amber-500/10 border border-amber-500/15 text-amber-300"
              }`}
            >
              <span className="text-[9px] text-zinc-500 font-semibold uppercase tracking-wider font-mono">
                {m.role === "user" ? "You" : "Vesper"}
              </span>
              <p className="text-xs leading-relaxed whitespace-pre-wrap">{m.content}</p>
            </div>
          ))
        )}
      </div>

      {/* Input controls */}
      <div className="p-4 border-t border-zinc-900 flex flex-col gap-4 bg-zinc-950">
        <div className="flex gap-2">
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSend()}
            placeholder={loading ? "Vesper is typing..." : "Ask Vesper to refine concept..."}
            disabled={loading}
            className="flex-1 bg-zinc-900 text-zinc-200 placeholder-zinc-600 border border-zinc-800 rounded-lg px-3 py-2 text-xs focus:outline-none focus:border-amber-400 transition"
          />
          <button
            onClick={handleSend}
            disabled={loading || !inputText.trim()}
            className="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 px-3 py-2 rounded-lg text-xs font-bold transition flex items-center justify-center border border-zinc-700 disabled:opacity-50"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* Generator panel */}
        <div className="border-t border-zinc-900 pt-4 flex flex-col gap-3">
          <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider font-mono">
            Creative Draft Generator
          </span>
          <div className="flex flex-col gap-2">
            <input
              type="text"
              value={topicText}
              onChange={(e) => setTopicText(e.target.value)}
              placeholder="e.g. 'The Leshy of Bialowieza'"
              className="w-full bg-zinc-900 text-zinc-200 placeholder-zinc-600 border border-zinc-800 rounded-lg px-3 py-2 text-xs focus:outline-none focus:border-amber-400 transition"
            />
            <div className="flex gap-2">
              <input
                type="number"
                value={beatsCount}
                onChange={(e) => setBeatsCount(e.target.value === "" ? "" : Number(e.target.value))}
                placeholder="Beats count"
                className="w-1/2 bg-zinc-900 text-zinc-200 placeholder-zinc-600 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-center focus:outline-none focus:border-amber-400 transition"
                min="1"
              />
              <button
                onClick={handleScriptFromChatClick}
                className="w-1/2 bg-zinc-905 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 rounded-lg text-[10px] font-bold shadow transition flex items-center justify-center gap-1"
              >
                <Sparkles className="h-3 w-3 text-amber-500" />
                <span>Use chat → script</span>
              </button>
            </div>
          </div>
          <button
            onClick={handleDraftClick}
            className="bg-amber-500 hover:bg-amber-600 text-zinc-950 w-full py-2.5 rounded-lg text-xs font-bold shadow-md shadow-amber-500/10 transition flex items-center justify-center gap-1.5"
          >
            <FileText className="h-3.5 w-3.5" />
            Draft Storyboard
          </button>
          <button
            onClick={onLockScript}
            disabled={scriptLocked}
            className={`w-full py-2 rounded-lg text-xs font-bold border transition flex items-center justify-center gap-1.5 ${
              scriptLocked
                ? "border-emerald-500/20 bg-emerald-950/20 text-emerald-400 cursor-not-allowed"
                : "border-zinc-800 bg-zinc-900 hover:bg-zinc-800 text-zinc-200"
            }`}
          >
            <Lock className="h-3.5 w-3.5" />
            <span>{scriptLocked ? "Script Locked ✓" : "🔒 Lock Script Narration"}</span>
          </button>
        </div>
      </div>
    </aside>
  );
}
