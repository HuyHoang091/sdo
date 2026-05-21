import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, Terminal as TerminalIcon } from 'lucide-react';
import { api } from '../api';

const LOG_SOURCES = [
  { id: 'agent', label: 'Agent Console' },
  { id: 'events', label: 'Agent Events' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'webhooks', label: 'Webhook Runs' },
];

export default function Terminal() {
  const [source, setSource] = useState('agent');
  const [lineLimit, setLineLimit] = useState(300);
  const [payload, setPayload] = useState({ lines: [], exists: false, updated_at: null, path: '' });
  const [isLoading, setIsLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [error, setError] = useState('');
  const viewportRef = useRef(null);

  const formattedLines = useMemo(() => {
    return (payload.lines || []).map((line) => {
      const trimmed = String(line || '');
      if (!trimmed) {
        return '';
      }
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return trimmed;
      }
    });
  }, [payload.lines]);

  const loadLogs = useCallback(async () => {
    setIsLoading(true);
    try {
      const response = await api.get('/api/agent/logs', {
        params: { source, limit: lineLimit },
      });
      setPayload(response.data || { lines: [], exists: false, updated_at: null, path: '' });
      setError('');
    } catch (err) {
      setError('Không tải được log source hiện tại.');
    } finally {
      setIsLoading(false);
    }
  }, [lineLimit, source]);

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!autoRefresh) {
      return undefined;
    }
    const timer = setInterval(loadLogs, 2500);
    return () => clearInterval(timer);
  }, [autoRefresh, loadLogs]);

  useEffect(() => {
    if (!autoScroll || !viewportRef.current) {
      return;
    }
    viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
  }, [formattedLines, autoScroll]);

  return (
    <div className="p-8 h-full flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-light text-white flex items-center gap-3">
            <TerminalIcon className="text-emerald-400" /> Agent Terminal & Debug Logs
          </h2>
          <p className="text-slate-400 mt-2">Theo dõi console agent, event bus, incident log và webhook runs theo thời gian thực để debug.</p>
        </div>
        <button onClick={loadLogs} className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm flex items-center gap-2">
          <RefreshCw size={16} className={isLoading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      <div className="flex flex-wrap gap-3 items-center">
        {LOG_SOURCES.map((item) => (
          <button
            key={item.id}
            onClick={() => setSource(item.id)}
            className={`px-4 py-2 rounded-lg text-sm border transition-colors ${source === item.id ? 'bg-emerald-500/10 border-emerald-500/50 text-emerald-300' : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'}`}
          >
            {item.label}
          </button>
        ))}

        <select value={lineLimit} onChange={(e) => setLineLimit(Number(e.target.value))} className="bg-slate-900 border border-slate-800 text-slate-300 rounded-lg px-3 py-2 text-sm">
          <option value={100}>100 lines</option>
          <option value={300}>300 lines</option>
          <option value={500}>500 lines</option>
          <option value={1000}>1000 lines</option>
        </select>

        <label className="inline-flex items-center gap-2 text-sm text-slate-400">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          <span>Auto refresh</span>
        </label>

        <label className="inline-flex items-center gap-2 text-sm text-slate-400">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
          <span>Auto scroll</span>
        </label>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <InfoCard label="Source File" value={payload.path || '--'} mono />
        <InfoCard label="Updated" value={payload.updated_at ? payload.updated_at.replace('T', ' ').substring(0, 19) : '--'} />
        <InfoCard label="Status" value={payload.exists ? 'Available' : 'Not created yet'} tone={payload.exists ? 'emerald' : 'amber'} />
      </div>

      {error && <div className="p-4 rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-300 text-sm">{error}</div>}

      <div className="flex-1 min-h-0 bg-slate-950 border border-slate-800 rounded-xl overflow-hidden shadow-xl">
        <div className="px-4 py-3 border-b border-slate-800 text-xs uppercase tracking-widest text-slate-500 flex justify-between">
          <span>{LOG_SOURCES.find((item) => item.id === source)?.label}</span>
          <span>{formattedLines.length} lines loaded</span>
        </div>
        <div ref={viewportRef} className="h-[calc(100vh-23rem)] overflow-auto p-4 font-mono text-xs leading-6 text-slate-200 whitespace-pre-wrap">
          {formattedLines.length === 0 ? (
            <div className="text-slate-500">{isLoading ? 'Loading logs...' : 'No log lines yet.'}</div>
          ) : (
            formattedLines.map((line, index) => (
              <div key={`${source}-${index}`} className="border-b border-slate-900/60 pb-2 mb-2 last:border-b-0">
                <span className="text-slate-600 mr-3">{String(index + 1).padStart(4, '0')}</span>
                <span>{line}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function InfoCard({ label, value, mono = false, tone = 'slate' }) {
  const toneClass = tone === 'emerald'
    ? 'text-emerald-300 border-emerald-500/20 bg-emerald-500/5'
    : tone === 'amber'
      ? 'text-amber-300 border-amber-500/20 bg-amber-500/5'
      : 'text-slate-200 border-slate-800 bg-slate-900/50';

  return (
    <div className={`p-4 rounded-xl border ${toneClass}`}>
      <div className="text-xs uppercase tracking-widest text-slate-500 mb-2">{label}</div>
      <div className={`text-sm ${mono ? 'font-mono break-all' : ''}`}>{value}</div>
    </div>
  );
}
