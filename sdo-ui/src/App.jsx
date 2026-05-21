import React from 'react';
import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Server, Settings, AlertTriangle, Blocks, PlaySquare, Terminal } from 'lucide-react';

import Overview from './pages/Overview';
import ServiceDetail from './pages/ServiceDetail';
import ConfigEditor from './pages/ConfigEditor';
import Incidents from './pages/Incidents';
import Webhooks from './pages/Webhooks';
import TrainingControl from './pages/TrainingControl';
import TerminalPage from './pages/Terminal';

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-slate-950 text-slate-200 overflow-hidden font-sans">
        {/* Sidebar */}
        <aside className="w-64 bg-slate-900 border-r border-slate-800 flex flex-col">
          <div className="p-5 border-b border-slate-800 flex items-center space-x-3">
             <Blocks className="text-emerald-400" />
             <h1 className="text-xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-emerald-400 to-cyan-400">
               SDO Control Plane
             </h1>
          </div>
          <nav className="flex-1 p-4 space-y-2">
            <NavItem to="/" icon={<LayoutDashboard />} label="Overview" />
            <NavItem to="/service" icon={<Server />} label="Service Detail" />
            <NavItem to="/incidents" icon={<AlertTriangle />} label="Incidents" />
            <NavItem to="/terminal" icon={<Terminal />} label="Terminal" />
            <NavItem to="/config" icon={<Settings />} label="Agent Config" />
            <NavItem to="/webhooks" icon={<PlaySquare />} label="Webhooks" />
            <NavItem to="/training" icon={<Blocks />} label="Training" />
          </nav>
        </aside>

        {/* Màn hình chính */}
        <main className="flex-1 overflow-auto bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-900 to-slate-950">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/service" element={<ServiceDetail />} />
            <Route path="/incidents" element={<Incidents />} />
            <Route path="/terminal" element={<TerminalPage />} />
            <Route path="/config" element={<ConfigEditor />} />
            <Route path="/webhooks" element={<Webhooks />} />
            <Route path="/training" element={<TrainingControl />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

function NavItem({ to, icon, label }) {
  const location = useLocation();
  const isActive = location.pathname === to;
  return (
    <Link 
      to={to} 
      className={`flex items-center space-x-3 p-3 rounded-lg transition-all duration-300 ${isActive ? 'bg-emerald-500/10 text-emerald-400' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'}`}
    >
      {React.cloneElement(icon, { size: 18 })}
      <span className="font-medium text-sm">{label}</span>
    </Link>
  );
}
