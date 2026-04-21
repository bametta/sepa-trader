const SIGNALS = {
  BREAKOUT:          { label: 'Breakout',       bg: 'bg-emerald-500/12', text: 'text-emerald-400', border: 'border-emerald-500/25', dot: 'bg-emerald-400 animate-pulse' },
  PULLBACK_EMA20:    { label: 'EMA 20',          bg: 'bg-blue-500/12',    text: 'text-blue-400',    border: 'border-blue-500/25',    dot: 'bg-blue-400' },
  PULLBACK_EMA50:    { label: 'EMA 50',          bg: 'bg-indigo-500/12',  text: 'text-indigo-400',  border: 'border-indigo-500/25',  dot: 'bg-indigo-400' },
  STAGE2_WATCH:      { label: 'Stage 2',         bg: 'bg-amber-500/12',   text: 'text-amber-400',   border: 'border-amber-500/25',   dot: 'bg-amber-400' },
  NO_SETUP:          { label: 'No Setup',        bg: 'bg-red-500/12',     text: 'text-red-400',     border: 'border-red-500/25',     dot: 'bg-red-400' },
  INSUFFICIENT_DATA: { label: 'No Data',         bg: 'bg-slate-500/12',   text: 'text-slate-400',   border: 'border-slate-500/25',   dot: 'bg-slate-400' },
}

export default function SignalBadge({ signal }) {
  const cfg = SIGNALS[signal]
  if (!cfg) return (
    <span className="inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide px-2 py-1 rounded-lg border bg-slate-500/10 text-slate-500 border-slate-500/20">
      {signal?.replace(/_/g, ' ') || 'N/A'}
    </span>
  )
  return (
    <span className={`inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide px-2 py-1 rounded-lg border ${cfg.bg} ${cfg.text} ${cfg.border}`}>
      <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${cfg.dot}`} />
      {cfg.label}
    </span>
  )
}
