import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Server, ArrowDownToLine, ArrowUpToLine, RotateCcw, Play, SquareX, Activity, AlertTriangle } from 'lucide-react';
import { api } from '../api';

export default function ServiceDetail() {
  const [overview, setOverview] = useState({ services: [], agent: { running: false, mode: 'none', command: null } });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refreshData = useCallback(async () => {
    try {
      const response = await api.get('/api/services/overview', { params: { limit: 8 } });
      setOverview(response.data || { services: [], agent: { running: false, mode: 'none', command: null } });
      setError('');
    } catch (err) {
      setError('Không tải được tổng quan services.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshData();
    const timer = setInterval(refreshData, 5000);
    return () => clearInterval(timer);
  }, [refreshData]);

  const defaultService = overview.services?.[0]?.service || null;

  const handleAgentAction = async (action, mode = 'live') => {
    await api.post('/api/agent/control', null, { params: { action, mode, service: defaultService } });
    setTimeout(refreshData, 1000);
  };

  const summary = useMemo(() => {
    const services = overview.services || [];
    return {
      total: services.length,
      incidents: services.reduce((sum, item) => sum + (item.recent_incidents?.length || 0), 0),
      avgCpu: services.length ? services.reduce((sum, item) => sum + (item.state?.cpu_percent || 0), 0) / services.length : 0,
      avgMem: services.length ? services.reduce((sum, item) => sum + (item.state?.memory_percent || 0), 0) / services.length : 0,
    };
  }, [overview.services]);

  if (loading) {
    return <div className="p-8 text-slate-400">Loading fleet overview...</div>;
  }

  return (
    <div className="p-8 space-y-8">
      <div className="flex justify-between items-start gap-4">
        <div>
          <h2 className="text-3xl font-light text-white flex items-center gap-3">
            <Server className="text-emerald-400" /> Fleet Service Overview
          </h2>
          <p className="text-slate-400 mt-2">Theo dõi toàn bộ service trong topology cùng lúc, không cần chuyển từng service riêng lẻ.</p>
          <div className="flex gap-4 mt-4 flex-wrap">
            <StatusPill label="Daemon" value={overview.agent?.running ? 'Running' : 'Stopped'} active={overview.agent?.running} />
            <StatusPill label="Mode" value={overview.agent?.mode || 'none'} />
            <StatusPill label="Orchestrator" value={overview.agent?.command || 'idle'} />
          </div>
        </div>
        <div className="flex gap-3 flex-wrap justify-end">
          {!overview.agent?.running ? (
            <>
              <button onClick={() => handleAgentAction('start', 'live')} className="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"><Play size={16} /> Start (Live RL)</button>
              <button onClick={() => handleAgentAction('start', 'dry-run')} className="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-sm font-medium transition-colors">Start (Dry-run)</button>
              <button onClick={() => handleAgentAction('start', 'rule-based')} className="px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded-lg text-sm font-medium transition-colors">Start (Rules)</button>
            </>
          ) : (
            <button onClick={() => handleAgentAction('stop')} className="px-5 py-2 bg-rose-600 hover:bg-rose-500 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 text-white">
              <SquareX size={16} /> Stop Agent
            </button>
          )}
        </div>
      </div>

      {error && <div className="p-4 rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-300 text-sm">{error}</div>}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <MetricBox label="Managed Services" value={summary.total} />
        <MetricBox label="Recent Incidents" value={summary.incidents} highlight={summary.incidents > 0} />
        <MetricBox label="Avg CPU" value={`${summary.avgCpu.toFixed(1)}%`} highlight={summary.avgCpu > 80} />
        <MetricBox label="Avg Memory" value={`${summary.avgMem.toFixed(1)}%`} highlight={summary.avgMem > 85} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {(overview.services || []).map((item) => (
          <div key={item.service} className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl space-y-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h3 className="text-xl text-white font-semibold flex items-center gap-2">
                  <Activity size={18} className="text-emerald-400" /> {item.service}
                </h3>
                <div className="flex gap-2 mt-3 flex-wrap">
                  <Tag>{item.config?.tier ? `Tier ${item.config.tier}` : 'No tier'}</Tag>
                  <Tag>{item.config?.scalable === false ? 'Non-scalable' : 'Scalable'}</Tag>
                  <Tag>{(item.config?.depends_on || []).length ? `Depends on: ${(item.config.depends_on || []).join(', ')}` : 'No dependencies'}</Tag>
                </div>
              </div>
              <div className={`px-3 py-2 rounded-lg text-sm ${item.recent_incidents?.length ? 'bg-rose-500/10 text-rose-300 border border-rose-500/30' : 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/30'}`}>
                {item.recent_incidents?.length ? `${item.recent_incidents.length} incident(s)` : 'Healthy'}
              </div>
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <MetricBox compact label="CPU" value={`${item.state?.cpu_percent?.toFixed?.(1) || '0.0'}%`} highlight={(item.state?.cpu_percent || 0) > 80} />
              <MetricBox compact label="Memory" value={`${item.state?.memory_percent?.toFixed?.(1) || '0.0'}%`} highlight={(item.state?.memory_percent || 0) > 85} />
              <MetricBox compact label="Replicas" value={`${item.state?.healthy_count || 0}/${item.state?.container_count || 0}`} highlight={(item.state?.healthy_count || 0) < (item.state?.container_count || 0)} />
              <MetricBox compact label="Error Rate" value={`${(((item.state?.error_rate || 0) * 100)).toFixed(1)}%`} highlight={(item.state?.error_rate || 0) > 0.1} />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <Panel title="Recent Actions">
                {(item.recent_actions || []).slice(0, 5).map((action, index) => (
                  <ActionRow key={`${item.service}-action-${index}`} action={action} />
                ))}
                {!(item.recent_actions || []).length && <EmptyText text="No recent actions." />}
              </Panel>

              <Panel title="Recent Incidents">
                {(item.recent_incidents || []).slice(0, 5).map((incident, index) => (
                  <IncidentRow key={`${item.service}-incident-${index}`} incident={incident} />
                ))}
                {!(item.recent_incidents || []).length && <EmptyText text="No recent incidents." />}
              </Panel>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StatusPill({ label, value, active = false }) {
  return (
    <span className={`px-3 py-1 text-xs rounded-full font-medium ${active ? 'bg-emerald-500/20 text-emerald-400' : 'bg-slate-800 text-slate-300'}`}>
      {label}: <span className="uppercase text-white font-bold">{value}</span>
    </span>
  );
}

function MetricBox({ label, value, highlight = false, compact = false }) {
  return (
    <div className={`rounded-xl border ${highlight ? 'border-rose-500/40 bg-rose-500/10 text-rose-300' : 'border-slate-800 bg-slate-900/50 text-slate-100'} ${compact ? 'p-4' : 'p-5'}`}>
      <div className="text-slate-400 text-sm mb-1">{label}</div>
      <div className={`${compact ? 'text-xl' : 'text-3xl'} font-bold`}>{value}</div>
    </div>
  );
}

function Tag({ children }) {
  return <span className="px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 text-xs">{children}</span>;
}

function Panel({ title, children }) {
  return (
    <div className="border border-slate-800 rounded-xl p-4 bg-slate-950/40 space-y-3">
      <h4 className="text-slate-300 font-medium">{title}</h4>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function ActionRow({ action }) {
  const name = action.action || 'no_op';
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3 text-sm">
      <div className="flex justify-between gap-3">
        <span className="text-slate-100 inline-flex items-center gap-2">
          {name.includes('_up') ? <ArrowUpToLine size={14} className="text-emerald-400" /> : null}
          {name.includes('_down') ? <ArrowDownToLine size={14} className="text-rose-400" /> : null}
          {name.includes('restart') ? <RotateCcw size={14} className="text-blue-400" /> : null}
          {name}
        </span>
        <span className="text-slate-500 font-mono">{action.time?.replace('T', ' ').substring(11, 19) || '--:--:--'}</span>
      </div>
      <div className="text-xs text-slate-500 mt-2">{action.decision_source || action.mode || 'unknown'} · CPU {(action.state?.cpu || 0).toFixed?.(1) || '0.0'}%</div>
    </div>
  );
}

function IncidentRow({ incident }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3 text-sm">
      <div className="flex items-start justify-between gap-3">
        <span className="text-slate-100 inline-flex items-center gap-2"><AlertTriangle size={14} className="text-amber-400" /> {incident.title || incident.category || 'Incident'}</span>
        <span className="text-slate-500 font-mono">{incident.time?.replace('T', ' ').substring(11, 19) || '--:--:--'}</span>
      </div>
      <div className="text-xs text-slate-400 mt-2">{incident.message || 'No message'}</div>
      <div className="text-xs text-slate-500 mt-2">{incident.category || 'general'} · {incident.source || 'unknown'}</div>
    </div>
  );
}

function EmptyText({ text }) {
  return <div className="text-sm text-slate-500">{text}</div>;
}
