import React from 'react';
import { Terminal, Zap, Sparkles, Server } from 'lucide-react';
export function NodeEnvironmentCard({ hostname, ip, healthy, cpu, ram }: { hostname: string; ip: string; healthy: boolean; cpu: number; ram: number }) {
  return (
    <div className="bg-[#12141D] border border-[#262A3B] rounded-lg p-4 space-y-3">
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-2">
          <Server className="w-4 h-4 text-[#22D3EE]" />
          <div>
            <div className="font-semibold text-sm text-[#F1F5F9]">{hostname}</div>
            <div className="font-mono-metrics text-xs text-[#64748B]">{ip}</div>
          </div>
        </div>
        <div className="flex gap-1">
          <button className="p-1.5 rounded bg-[#1B1E2B] text-[#22D3EE] border border-[#262A3B]"><Terminal className="w-3.5 h-3.5" /></button>
          <button className="p-1.5 rounded bg-[#1B1E2B] text-[#10B981] border border-[#262A3B]"><Zap className="w-3.5 h-3.5" /></button>
          <button className="p-1.5 rounded bg-[#1B1E2B] text-[#818CF8] border border-[#262A3B]"><Sparkles className="w-3.5 h-3.5" /></button>
        </div>
      </div>
      <div className={`px-2.5 py-1 rounded text-xs border ${healthy ? 'bg-[#10B981]/10 border-[#10B981]/30 text-[#10B981]' : 'bg-[#F59E0B]/10 border-[#F59E0B]/30 text-[#F59E0B]'}`}>
        {healthy ? '✓ All services healthy' : '⚠ Attention required'}
      </div>
      <div className="space-y-1.5 text-xs font-mono-metrics">
        <div className="flex justify-between"><span className="text-[#64748B]">CPU</span><span>{cpu}%</span></div>
        <div className="w-full h-1.5 bg-[#1B1E2B] rounded-full"><div className="h-full bg-[#22D3EE] rounded-full" style={{ width: `${cpu}%` }} /></div>
        <div className="flex justify-between"><span className="text-[#64748B]">RAM</span><span>{ram}%</span></div>
        <div className="w-full h-1.5 bg-[#1B1E2B] rounded-full"><div className="h-full bg-[#10B981] rounded-full" style={{ width: `${ram}%` }} /></div>
      </div>
    </div>
  );
}
