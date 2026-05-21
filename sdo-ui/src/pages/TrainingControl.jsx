import React, { useEffect, useState, useRef } from 'react';
import { Play, DownloadCloud, Box, Terminal, SquareTerminal } from 'lucide-react';
import { api, buildWsUrl } from '../api';

export default function TrainingControl() {
  const [logs, setLogs] = useState([]);
  const ws = useRef(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    ws.current = new WebSocket(buildWsUrl('/api/training/stream'));
    ws.current.onmessage = (event) => setLogs(prev => [...prev, event.data]);
    return () => ws.current?.close();
  }, []);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [logs]);

  const handleAction = async (taskName) => {
    setLogs([]); // Clear logs on new run
    await api.post('/api/training/control', null, { params: { action: taskName } });
  };

  const stopTask = async () => {
    await api.post('/api/training/control', null, { params: { action: 'stop' } });
  };

  return (
    <div className="p-8 flex flex-col h-full">
      <h2 className="text-3xl font-light text-white mb-6">Agent Operations & Operations Terminal</h2>
      
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
        <ActionCard onClick={() => handleAction('chaos')} title="Chaos Collection" btnClr="bg-blue-600 hover:bg-blue-500" icon={<Box/>} />
        <ActionCard onClick={() => handleAction('train')} title="Live Model Training" btnClr="bg-purple-600 hover:bg-purple-500" icon={<Play/>} />
        <ActionCard onClick={() => handleAction('export-onnx')} title="Export ONNX" btnClr="bg-teal-600 hover:bg-teal-500" icon={<DownloadCloud/>} />
      </div>

      {/* TERMINAL UI */}
      <div className="flex-1 bg-[#1e1e1e] border border-slate-700/50 rounded-xl overflow-hidden shadow-2xl flex flex-col min-h-[400px]">
          <div className="bg-[#2d2d2d] border-b border-[#404040] px-4 py-2 flex justify-between items-center shadow">
             <div className="flex items-center text-slate-300 font-sans font-medium text-sm">
                 <Terminal size={14} className="mr-2 text-emerald-500"/> PTY Terminal :: System Runner
             </div>
             <button onClick={stopTask} className="text-rose-400 hover:text-white hover:bg-rose-500/20 px-3 py-1 rounded transition-colors flex items-center text-xs font-mono">
                [SIGINT] Stop (Ctrl+C)
             </button>
          </div>
          <div ref={scrollRef} className="p-5 flex-1 overflow-y-auto font-mono text-[13px] leading-relaxed text-[#00ff00] whitespace-pre-wrap selection:bg-white/20">
              {logs.length === 0 ? "PS C:\\SDO> Ready. Waiting for signals...\n" : logs.join('')}
              <span className="animate-pulse">_</span>
          </div>
      </div>
    </div>
  );
}

function ActionCard({ icon, title, btnClr, onClick }) {
  return (
    <div className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 flex items-center justify-between">
      <div className="flex items-center gap-4">
        <div className="p-3 bg-slate-800/80 rounded-lg text-slate-300">{icon}</div>
        <h3 className="text-sm font-medium text-slate-100">{title}</h3>
      </div>
      <button onClick={onClick} className={`px-4 py-2 text-xs rounded-lg text-white font-medium transition-all ${btnClr}`}>Execute</button>
    </div>
  );
}
