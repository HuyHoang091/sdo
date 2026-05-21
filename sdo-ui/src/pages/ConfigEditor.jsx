import React, { useEffect, useState } from 'react';
import { Save, AlertTriangle } from 'lucide-react';
import { api } from '../api';

export default function ConfigEditor() {
  const [services, setServices] = useState([]);
  const [selectedService, setSelectedService] = useState(null);
  const [config, setConfig] = useState(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    let isMounted = true;

    const loadServices = async () => {
      try {
        const response = await api.get('/api/services');
        if (!isMounted) {
          return;
        }

        const serviceItems = response.data?.services || [];
        setServices(serviceItems);
        setSelectedService(response.data?.default_service || serviceItems[0]?.name || null);
      } catch (e) {
        setError('❌ Failed to load service list!');
      }
    };

    loadServices();
    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (!selectedService) {
      return;
    }

    api
      .get('/api/service', { params: { service: selectedService } })
      .then((res) => {
        setConfig(res.data.config);
        setError('');
      })
      .catch(() => {
        setError('❌ Failed to load config from sdo.yml!');
      });
  }, [selectedService]);

  const handleSave = async () => {
    if (!config) {
      return;
    }

    if (config.max_containers < config.min_containers) {
      setError('❌ Lỗi: Max Containers không được nhỏ hơn Min Containers.');
      setSuccess(false);
      return;
    }

    setError('');
    try {
      await api.post('/api/service/config', config, { params: { service: selectedService } });
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
    } catch (e) {
      setError('❌ Failed to save config to sdo.yml!');
    }
  };

  if (!config) return <div className="p-8">Loading Agent Policy...</div>;

  return (
    <div className="p-8 max-w-4xl">
      <div className="flex justify-between items-center mb-6 gap-4">
        <div>
          <h2 className="text-3xl font-light text-white">Rule Policy & Configuration</h2>
          <p className="text-slate-400 mt-2">
            Editing service: <span className="font-mono text-white">{selectedService || '...'}</span>
          </p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={selectedService || ''}
            onChange={(e) => setSelectedService(e.target.value)}
            className="bg-slate-900 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 text-sm min-w-48"
          >
            {services.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}{item.tier ? ` · Tier ${item.tier}` : ''}
              </option>
            ))}
          </select>
          <button onClick={handleSave} className="flex items-center space-x-2 bg-emerald-600 hover:bg-emerald-500 text-white px-5 py-2 rounded-md font-medium transition-colors">
            <Save size={18} /><span>Save to sdo.yml</span>
          </button>
        </div>
      </div>

      {error && <div className="mb-6 p-4 bg-rose-500/10 border border-rose-500 text-rose-400 rounded-lg flex items-center"><AlertTriangle size={18} className="mr-2" />{error}</div>}
      {success && <div className="mb-6 p-4 bg-emerald-500/10 border border-emerald-500 text-emerald-400 rounded-lg">✅ Configuration Applied Successfully!</div>}

      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-6 shadow-xl">
        <div className="grid grid-cols-2 gap-6">
          <InputGroup label="Min Containers" type="number" value={config.min_containers} onChange={(v) => setConfig({ ...config, min_containers: parseInt(v, 10) || 0 })} />
          <InputGroup label="Max Containers" type="number" value={config.max_containers} onChange={(v) => setConfig({ ...config, max_containers: parseInt(v, 10) || 0 })} />

          <InputGroup label="CPU High Threshold (%)" type="number" value={config.cpu_high} onChange={(v) => setConfig({ ...config, cpu_high: parseInt(v, 10) || 0 })} />
          <InputGroup label="CPU Low Threshold (%)" type="number" value={config.cpu_low} onChange={(v) => setConfig({ ...config, cpu_low: parseInt(v, 10) || 0 })} />

          <InputGroup label="Action Cooldown (seconds)" type="number" value={config.cooldown_seconds ?? ''} onChange={(v) => setConfig({ ...config, cooldown_seconds: parseInt(v, 10) || 0 })} />
          <InputGroup label="Rollback Window (seconds)" type="number" value={config.rollback_window_seconds ?? ''} onChange={(v) => setConfig({ ...config, rollback_window_seconds: parseInt(v, 10) || 0 })} />

          <InputGroup label="Memory Danger (%)" type="number" value={config.mem_danger ?? ''} onChange={(v) => setConfig({ ...config, mem_danger: parseInt(v, 10) || 0 })} />
          <InputGroup label="Memory Warning (%)" type="number" value={config.mem_warning ?? ''} onChange={(v) => setConfig({ ...config, mem_warning: parseInt(v, 10) || 0 })} />

          <InputGroup label="Rollback Error Threshold" type="number" value={config.rollback_error_threshold ?? 0.1} onChange={(v) => setConfig({ ...config, rollback_error_threshold: parseFloat(v) || 0 })} />
          <InputGroup label="Rollback Latency Factor" type="number" value={config.rollback_latency_factor ?? 2} onChange={(v) => setConfig({ ...config, rollback_latency_factor: parseFloat(v) || 0 })} />

          <InputGroup label="Tier" type="number" value={config.tier ?? ''} onChange={(v) => setConfig({ ...config, tier: v === '' ? null : parseInt(v, 10) || 0 })} />
          <InputGroup label="Depends On (comma separated)" type="text" value={(config.depends_on || []).join(',')} onChange={(v) => setConfig({ ...config, depends_on: v.split(',').map((item) => item.trim()).filter(Boolean) })} />
        </div>

        <div className="flex items-center justify-between border border-slate-800 rounded-lg p-4">
          <div>
            <div className="text-slate-200 font-medium">Scalable service</div>
            <div className="text-slate-500 text-sm">Disable if this service should not be scaled horizontally.</div>
          </div>
          <label className="inline-flex items-center gap-3 text-slate-200">
            <input type="checkbox" checked={config.scalable ?? true} onChange={(e) => setConfig({ ...config, scalable: e.target.checked })} />
            <span>{(config.scalable ?? true) ? 'Enabled' : 'Disabled'}</span>
          </label>
        </div>
      </div>
    </div>
  );
}

function InputGroup({ label, value, type, onChange }) {
  return (
    <div>
      <label className="block text-sm font-medium text-slate-400 mb-2">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-slate-950 border border-slate-700 rounded-lg p-3 text-slate-200 outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all"
      />
    </div>
  );
}
