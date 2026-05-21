import React, { useEffect, useMemo, useState } from 'react';
import { AlertOctagon, AlertTriangle, Bug, ShieldAlert } from 'lucide-react';
import { api } from '../api';

export default function Incidents() {
  const [incidents, setIncidents] = useState([]);
  const [serviceFilter, setServiceFilter] = useState('all');
  const [severityFilter, setSeverityFilter] = useState('all');
  const [categoryFilter, setCategoryFilter] = useState('all');

  useEffect(() => {
    api.get('/api/incidents', { params: { limit: 100, service: 'all' } }).then((res) => setIncidents(res.data || []));
  }, []);

  const services = useMemo(() => ['all', ...Array.from(new Set((incidents || []).map((item) => item.service).filter(Boolean)))], [incidents]);
  const categories = useMemo(() => ['all', ...Array.from(new Set((incidents || []).map((item) => item.category).filter(Boolean)))], [incidents]);

  const filteredIncidents = useMemo(() => {
    return (incidents || []).filter((item) => {
      if (serviceFilter !== 'all' && item.service !== serviceFilter) return false;
      if (severityFilter !== 'all' && item.severity !== severityFilter) return false;
      if (categoryFilter !== 'all' && item.category !== categoryFilter) return false;
      return true;
    });
  }, [incidents, serviceFilter, severityFilter, categoryFilter]);

  return (
    <div className="p-8 space-y-6">
      <div>
        <h2 className="text-3xl font-light text-white mb-2">Incident History</h2>
        <p className="text-slate-400">Bao gồm rollback, degraded state, và lỗi runtime agent đã được ghi lại qua event pipeline.</p>
      </div>

      <div className="flex flex-wrap gap-3">
        <select value={serviceFilter} onChange={(e) => setServiceFilter(e.target.value)} className="bg-slate-900 border border-slate-800 text-slate-300 rounded-lg px-3 py-2 text-sm">
          {services.map((service) => <option key={service} value={service}>{service}</option>)}
        </select>
        <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)} className="bg-slate-900 border border-slate-800 text-slate-300 rounded-lg px-3 py-2 text-sm">
          <option value="all">All severities</option>
          <option value="critical">Critical</option>
          <option value="warning">Warning</option>
        </select>
        <select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)} className="bg-slate-900 border border-slate-800 text-slate-300 rounded-lg px-3 py-2 text-sm">
          {categories.map((category) => <option key={category} value={category}>{category}</option>)}
        </select>
      </div>

      <div className="grid gap-4">
        {filteredIncidents.map((inc, i) => {
          const isCritical = inc.severity === 'critical';
          const icon = inc.category === 'agent_error'
            ? <Bug className="text-rose-400" />
            : inc.category === 'rollback'
              ? <ShieldAlert className="text-rose-400" />
              : isCritical
                ? <AlertOctagon className="text-rose-400" />
                : <AlertTriangle className="text-amber-400" />;

          return (
            <div key={i} className={`p-5 rounded-xl border shadow-sm bg-slate-900 ${isCritical ? 'border-rose-900/50' : 'border-slate-800'}`}>
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start space-x-4 flex-1">
                  <div className="mt-1">{icon}</div>
                  <div className="space-y-2 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h4 className="text-lg font-semibold text-slate-200">{inc.title || 'Incident'}</h4>
                      <span className={`px-2 py-1 rounded-full text-xs ${isCritical ? 'bg-rose-500/10 text-rose-300' : 'bg-amber-500/10 text-amber-300'}`}>{inc.severity || 'warning'}</span>
                      <span className="px-2 py-1 rounded-full text-xs bg-slate-800 text-slate-300">{inc.category || 'general'}</span>
                      <span className="px-2 py-1 rounded-full text-xs bg-slate-800 text-slate-300">source: {inc.source || 'action_log'}</span>
                    </div>
                    <p className="text-sm text-slate-300">{inc.message || 'No message captured.'}</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm text-slate-400">
                      <p>Service: <span className="font-mono bg-slate-800 px-1 rounded">{inc.service || 'unknown'}</span></p>
                      <p>Action: <span className="font-mono bg-slate-800 px-1 rounded">{inc.action || 'n/a'}</span></p>
                      <p>CPU: {(inc.state?.cpu || 0).toFixed?.(1) || '0.0'}%</p>
                      <p>Error Rate: {((inc.state?.error_rate || 0) * 100).toFixed(1)}%</p>
                    </div>
                    {inc.data && Object.keys(inc.data).length > 0 && (
                      <pre className="mt-3 text-xs text-slate-400 bg-slate-950 border border-slate-800 rounded-lg p-3 overflow-auto whitespace-pre-wrap">{JSON.stringify(inc.data, null, 2)}</pre>
                    )}
                  </div>
                </div>
                <div className="text-right text-sm text-slate-400 font-mono min-w-40">
                  {inc.time ? inc.time.replace('T', ' ').substring(0, 19) : '--'}
                </div>
              </div>
            </div>
          );
        })}

        {filteredIncidents.length === 0 && <div className="text-slate-500 text-center py-10">No incidents matched your current filters.</div>}
      </div>
    </div>
  );
}
