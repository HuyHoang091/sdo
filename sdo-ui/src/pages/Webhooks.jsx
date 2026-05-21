import React, { useEffect, useState } from 'react';
import { Save, Plus, Trash2, Link, Workflow, Wand2 } from 'lucide-react';
import { api } from '../api';

const DEFAULT_HOOK = () => ({
  id: Date.now().toString(),
  name: 'New Workflow',
  description: '',
  events: ['rollback_triggered'],
  variables: '{\n  "auth_base": "https://example.com",\n  "notify_channel": "ops"\n}',
  steps: [],
});

const DEFAULT_STEP = () => ({
  id: `step_${Date.now()}`,
  name: 'Request Token',
  enabled: true,
  url: 'https://example.com/api/token',
  method: 'POST',
  headers: '{\n  "Content-Type": "application/json"\n}',
  body: '{\n  "service": "{{service}}",\n  "channel": "{{workflow.variables.notify_channel}}"\n}',
  body_mode: 'json',
  extract: '{\n  "access_token": "json.access_token"\n}',
  condition: '',
  timeout_seconds: 5,
  continue_on_error: false,
});

export default function Webhooks() {
  const [hooks, setHooks] = useState([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.get('/api/webhooks').then((res) => setHooks(res.data || []));
  }, []);

  const updateHook = (hookIndex, updater) => {
    setHooks((current) => current.map((hook, index) => (index === hookIndex ? updater({ ...hook }) : hook)));
  };

  const addHook = () => setHooks((current) => [...current, DEFAULT_HOOK()]);

  const addStep = (hookIndex) => {
    updateHook(hookIndex, (hook) => ({ ...hook, steps: [...(hook.steps || []), DEFAULT_STEP()] }));
  };

  const saveHooks = async () => {
    setSaving(true);
    try {
      await api.post('/api/webhooks', hooks);
      window.alert('Saved workflow configuration.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="p-8 max-w-7xl space-y-6">
      <div className="flex justify-between items-center gap-4">
        <div>
          <h2 className="text-3xl font-light text-white flex items-center gap-3"><Workflow className="text-emerald-400" /> Stateful Webhook Workflows</h2>
          <p className="text-slate-400 mt-2">Hỗ trợ multi-step workflow: lấy token, extract dữ liệu response, rồi dùng lại ở step sau qua template variables.</p>
        </div>
        <button onClick={saveHooks} className="bg-emerald-600 hover:bg-emerald-500 text-white px-5 py-2 rounded flex gap-2 items-center" disabled={saving}>
          <Save size={18} /> {saving ? 'Saving...' : 'Save Config'}
        </button>
      </div>

      <div className="rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-4 text-sm text-cyan-100">
        <div className="font-semibold mb-2 flex items-center gap-2"><Wand2 size={16} /> Template examples</div>
        <div className="grid md:grid-cols-2 gap-2 text-xs font-mono text-cyan-200/90">
          <div>{'{{service}} · {{event}} · {{time}}'}</div>
          <div>{'{{workflow.variables.access_token}} · {{steps.step_1.json.id}}'}</div>
          <div>{'{{payload.action}} · {{data.reason}}'}</div>
          <div>{'{{workflow.last.status_code}} · {{workflow.last.text}}'}</div>
        </div>
      </div>

      <div>
        <button onClick={addHook} className="flex px-4 py-2 border border-blue-500 text-blue-400 hover:bg-blue-500/10 rounded items-center gap-2"><Plus size={16} /> New Workflow</button>
      </div>

      <div className="space-y-6">
        {hooks.map((hook, hookIndex) => (
          <div key={hook.id || hookIndex} className="bg-slate-900 border border-slate-700 rounded-xl p-6 space-y-5">
            <div className="flex justify-between items-start gap-4">
              <div className="flex-1 space-y-3">
                <input
                  value={hook.name || ''}
                  onChange={(e) => updateHook(hookIndex, (current) => ({ ...current, name: e.target.value }))}
                  className="w-full bg-transparent text-2xl font-semibold text-slate-100 outline-none border-b border-transparent focus:border-slate-600"
                />
                <textarea
                  rows={2}
                  value={hook.description || ''}
                  onChange={(e) => updateHook(hookIndex, (current) => ({ ...current, description: e.target.value }))}
                  placeholder="Describe what this workflow does..."
                  className="w-full bg-slate-950 border border-slate-800 p-3 rounded text-sm text-slate-300"
                />
              </div>
              <button onClick={() => setHooks((current) => current.filter((_, index) => index !== hookIndex))} className="text-rose-400 hover:bg-rose-500/20 p-2 rounded"><Trash2 size={18} /></button>
            </div>

            <div className="grid lg:grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-slate-400 uppercase tracking-widest font-bold">Trigger Events</label>
                <input
                  value={(hook.events || []).join(',')}
                  onChange={(e) => updateHook(hookIndex, (current) => ({ ...current, events: e.target.value.split(',').map((item) => item.trim()).filter(Boolean) }))}
                  className="w-full bg-slate-950 border border-slate-800 p-2 rounded text-sm text-slate-300 mt-1"
                />
              </div>
              <div>
                <label className="text-xs text-slate-400 uppercase tracking-widest font-bold">Workflow Variables (JSON)</label>
                <textarea
                  rows={4}
                  value={hook.variables || '{}'}
                  onChange={(e) => updateHook(hookIndex, (current) => ({ ...current, variables: e.target.value }))}
                  className="w-full bg-slate-950 border border-slate-800 p-2 rounded text-sm text-emerald-300 mt-1 font-mono"
                />
              </div>
            </div>

            <div className="pl-4 border-l-2 border-slate-800 space-y-4">
              {(hook.steps || []).map((step, stepIndex) => (
                <div key={step.id || stepIndex} className="bg-slate-950 p-4 rounded border border-slate-800 relative group space-y-4">
                  <div className="absolute -left-[26px] top-6 bg-slate-800 rounded-full p-1"><Link size={12} className="text-slate-400" /></div>
                  <div className="flex justify-between items-start gap-4">
                    <div className="flex-1 grid md:grid-cols-2 gap-3">
                      <input
                        value={step.id || ''}
                        onChange={(e) => updateHook(hookIndex, (current) => {
                          const next = [...(current.steps || [])];
                          next[stepIndex] = { ...next[stepIndex], id: e.target.value };
                          return { ...current, steps: next };
                        })}
                        placeholder="step_id"
                        className="bg-slate-900 border border-slate-700 rounded p-2 text-xs text-cyan-300 font-mono"
                      />
                      <input
                        value={step.name || ''}
                        onChange={(e) => updateHook(hookIndex, (current) => {
                          const next = [...(current.steps || [])];
                          next[stepIndex] = { ...next[stepIndex], name: e.target.value };
                          return { ...current, steps: next };
                        })}
                        placeholder="Readable step name"
                        className="bg-slate-900 border border-slate-700 rounded p-2 text-sm text-slate-200"
                      />
                    </div>
                    <button onClick={() => updateHook(hookIndex, (current) => ({ ...current, steps: (current.steps || []).filter((_, index) => index !== stepIndex) }))} className="text-rose-500 text-sm">Remove Step</button>
                  </div>

                  <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
                    <select value={step.method || 'POST'} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'method', e.target.value)} className="bg-slate-900 border border-slate-700 text-slate-300 rounded p-2 text-sm">
                      <option>POST</option>
                      <option>GET</option>
                      <option>PUT</option>
                      <option>PATCH</option>
                      <option>DELETE</option>
                    </select>
                    <select value={step.body_mode || 'json'} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'body_mode', e.target.value)} className="bg-slate-900 border border-slate-700 text-slate-300 rounded p-2 text-sm">
                      <option value="json">JSON Body</option>
                      <option value="text">Text Body</option>
                    </select>
                    <input type="number" min="1" value={step.timeout_seconds ?? 5} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'timeout_seconds', Number(e.target.value || 5))} className="bg-slate-900 border border-slate-700 text-slate-300 rounded p-2 text-sm" placeholder="Timeout seconds" />
                    <label className="inline-flex items-center gap-2 text-sm text-slate-400 bg-slate-900 border border-slate-700 rounded p-2">
                      <input type="checkbox" checked={step.continue_on_error || false} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'continue_on_error', e.target.checked)} />
                      Continue on error
                    </label>
                  </div>

                  <div className="grid grid-cols-1 gap-3">
                    <input value={step.url || ''} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'url', e.target.value)} placeholder="https://endpoint-url or {{workflow.variables.auth_base}}/token" className="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-emerald-400 font-mono" />
                    <input value={step.condition || ''} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'condition', e.target.value)} placeholder="Optional condition, e.g. {{event}} == rollback_triggered -> use non-empty/falsey text evaluation" className="w-full bg-slate-900 border border-slate-700 rounded p-2 text-sm text-slate-300 font-mono" />
                  </div>

                  <div className="grid lg:grid-cols-3 gap-4">
                    <JsonEditor title="Headers (JSON)" value={step.headers || '{}'} onChange={(value) => mutateStep(setHooks, hookIndex, stepIndex, 'headers', value)} tone="text-slate-300" />
                    <JsonEditor title="Body Template" value={step.body || ''} onChange={(value) => mutateStep(setHooks, hookIndex, stepIndex, 'body', value)} tone="text-amber-200" />
                    <JsonEditor title="Extract Map (JSON)" value={step.extract || '{}'} onChange={(value) => mutateStep(setHooks, hookIndex, stepIndex, 'extract', value)} tone="text-cyan-300" />
                  </div>

                  <label className="inline-flex items-center gap-2 text-sm text-slate-400">
                    <input type="checkbox" checked={step.enabled !== false} onChange={(e) => mutateStep(setHooks, hookIndex, stepIndex, 'enabled', e.target.checked)} />
                    Step enabled
                  </label>
                </div>
              ))}
              <button onClick={() => addStep(hookIndex)} className="text-slate-400 hover:text-white text-sm flex items-center gap-1"><Plus size={16} /> Add Action Step</button>
            </div>
          </div>
        ))}
        {hooks.length === 0 && <div className="text-slate-500 text-center py-10">No workflows defined. Create one to chain auth, token retrieval, and downstream actions.</div>}
      </div>
    </div>
  );
}

function mutateStep(setHooks, hookIndex, stepIndex, field, value) {
  setHooks((current) => current.map((hook, index) => {
    if (index !== hookIndex) return hook;
    const nextSteps = [...(hook.steps || [])];
    nextSteps[stepIndex] = { ...nextSteps[stepIndex], [field]: value };
    return { ...hook, steps: nextSteps };
  }));
}

function JsonEditor({ title, value, onChange, tone }) {
  return (
    <div>
      <span className="text-xs text-slate-500 mb-1 block">{title}</span>
      <textarea rows={6} value={value} onChange={(e) => onChange(e.target.value)} className={`w-full bg-slate-900 text-xs font-mono border border-slate-700 rounded p-2 ${tone}`} />
    </div>
  );
}
