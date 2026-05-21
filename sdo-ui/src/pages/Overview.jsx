import React, { useEffect, useState } from 'react';
import { Activity, Server, AlertCircle } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { api } from '../api';

export default function Overview() {
  const [summary, setSummary] = useState(null);
  const [actions, setActions] = useState([]);

  useEffect(() => {
    let isMounted = true;
    const fetcher = async () => {
      try {
        const [sumRes, actRes] = await Promise.all([
          api.get('/api/system/summary'),
          api.get('/api/service/actions', { params: { limit: 50, service: 'all' } })
        ]);
        if (!isMounted) return;
        setSummary(sumRes.data);
        const data = (actRes.data || []).map(act => ({
          time: act.time?.split('T')[1]?.substring(0, 5) || '--:--',
          cpu: act.state?.cpu || 0,
          action: act.action,
          service: act.service || 'unknown'
        })).reverse();
        setActions(data);
      } catch (err) { }
    };

    fetcher();
    const iv = setInterval(fetcher, 5000);
    return () => {
      isMounted = false;
      clearInterval(iv);
    };
  }, []);

  return (
    <div className="p-8">
      <h2 className="text-3xl font-light text-white mb-6">Cluster Overview</h2>

      {/* Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <StatCard icon={<Activity className="text-cyan-400" />} title="Host Resources" value={summary ? `${(100 - summary.host_cpu_available).toFixed(1)}% CPU` : '...'} subtitle="Current Load" />
        <StatCard icon={<Server className="text-purple-400" />} title="Agent Mode" value={summary?.agent_mode ? summary.agent_mode.toUpperCase() : '...'} subtitle={`${summary?.total_services_managed || 0} services handled`} />
        <StatCard icon={<AlertCircle className="text-rose-500" />} title="Active Alerts" value={summary?.active_alerts || 0} subtitle={`${summary?.recent_rollbacks || 0} recent rollbacks`} />
      </div>

      {/* Chart */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-2xl">
        <h3 className="text-lg font-medium mb-4 text-slate-300">Action History & CPU Impact (Last 50 triggers)</h3>
        <div className="h-64">
          {actions.length > 0 && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={actions}>
                <XAxis dataKey="time" stroke="#475569" />
                <YAxis stroke="#475569" />
                <Tooltip contentStyle={{ backgroundColor: '#0f172a', border: 'none', borderRadius: '8px' }} />
                <Line type="monotone" dataKey="cpu" stroke="#10b981" strokeWidth={3} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}

function StatCard({ icon, title, value, subtitle }) {
  return (
    <div className="bg-slate-900/50 backdrop-blur-md border border-slate-800 rounded-xl p-6 flex items-start space-x-4">
      <div className="p-3 bg-slate-800 rounded-lg">{icon}</div>
      <div>
        <h4 className="text-slate-400 text-sm font-medium">{title}</h4>
        <div className="text-2xl font-bold text-slate-100 mt-1">{value}</div>
        <p className="text-slate-500 text-xs mt-1">{subtitle}</p>
      </div>
    </div>
  );
}
