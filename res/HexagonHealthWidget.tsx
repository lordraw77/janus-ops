import React from 'react';
export function HexagonHealthWidget({ title, up, downtime, unreachable, down }: { title: string; up: number; downtime: number; unreachable: number; down: number }) {
  const total = up + downtime + unreachable + down;
  const pct = total > 0 ? Math.round((up / total) * 100) : 100;
  const innerStroke = down > 0 ? '#EF4444' : unreachable > 0 ? '#F59E0B' : '#10B981';
  return (
    <div className="bg-[#12141D] border border-[#262A3B] rounded-lg p-4 flex items-center justify-between gap-4">
      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-[#64748B] mb-2">{title}</div>
        <div className="relative w-28 h-28 flex items-center justify-center">
          <svg viewBox="0 0 120 120" className="w-full h-full">
            <polygon points="60,6 108,33 108,87 60,114 12,87 12,33" fill="rgba(16,185,129,0.08)" stroke="#10B981" strokeWidth="4" />
            <polygon points="60,20 96,40 96,80 60,100 24,80 24,40" fill="rgba(34,211,238,0.06)" stroke="#22D3EE" strokeWidth="3" />
            <polygon points="60,34 84,47 84,73 60,86 36,73 36,47" fill="rgba(239,68,68,0.18)" stroke={innerStroke} strokeWidth="3" />
          </svg>
          <span className="absolute font-mono-metrics text-sm font-bold text-[#F1F5F9]">{pct}%</span>
        </div>
      </div>
      <div className="flex-1 space-y-1.5 text-xs font-mono-metrics">
        <div className="flex justify-between"><span className="text-[#10B981]">● Up / OK</span><strong>{up}</strong></div>
        <div className="flex justify-between"><span className="text-[#22D3EE]">● Downtime</span><span>{downtime}</span></div>
        <div className="flex justify-between"><span className="text-[#F59E0B]">● Unreachable</span><span>{unreachable}</span></div>
        <div className="flex justify-between"><span className="text-[#EF4444]">● Down / Crit</span><strong className="text-[#EF4444]">{down}</strong></div>
      </div>
    </div>
  );
}
