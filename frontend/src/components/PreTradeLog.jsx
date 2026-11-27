import { Fragment, useState } from 'react'
import { useQuery } from 'react-query'
import { fetchPreTradeLog } from '../api/client'

const VERDICT_STYLES = {
  PROCEED: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300',
  WARN:    'bg-amber-500/10  border-amber-500/30  text-amber-300',
  ABORT:   'bg-red-500/10    border-red-500/30    text-red-300',
  HOLD:    'bg-slate-500/10  border-slate-500/30  text-slate-300',
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export default function PreTradeLog() {
  const [expanded, setExpanded] = useState(null)
  const { data: rows = [], isLoading, isError } = useQuery(
    'pre-trade-log',
    () => fetchPreTradeLog(100),
    { refetchInterval: 30_000, staleTime: 10_000 },
  )

  if (isLoading) return <div className="card p-12 text-center text-slate-500 text-sm">Loading pre-trade analyses…</div>
  if (isError)   return <div className="card p-12 text-center text-red-400 text-sm">Failed to load pre-trade log.</div>
  if (!rows.length) {
    return (
      <div className="card p-16 text-center">
        <p className="text-4xl mb-4 opacity-20">⚙</p>
        <p className="text-slate-500 text-sm">No pre-trade AI analyses yet.</p>
        <p className="text-slate-600 text-xs mt-2">
          The gate logs to this table every time a buy is evaluated. If empty, no buys
          have been attempted in this mode yet (or the AI key is unset).
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Pre-trade AI gate — last {rows.length}</h2>
        <span className="text-[11px] text-slate-500">Auto-refresh 30s</span>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-[11px] uppercase tracking-wide text-slate-500 bg-white/2 border-b border-white/5">
            <tr>
              <th className="text-left px-3 py-2 font-semibold">Time</th>
              <th className="text-left px-3 py-2 font-semibold">Symbol</th>
              <th className="text-left px-3 py-2 font-semibold">Trigger</th>
              <th className="text-left px-3 py-2 font-semibold">Verdict</th>
              <th className="text-left px-3 py-2 font-semibold">Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const isOpen = expanded === r.id
              const style = VERDICT_STYLES[r.verdict?.toUpperCase()] || VERDICT_STYLES.HOLD
              return (
                <Fragment key={r.id}>
                  <tr
                    onClick={() => setExpanded(isOpen ? null : r.id)}
                    className="border-b border-white/5 hover:bg-white/3 cursor-pointer transition-colors"
                  >
                    <td className="px-3 py-2 text-slate-400 text-[12px] whitespace-nowrap">{fmtTime(r.created_at)}</td>
                    <td className="px-3 py-2 font-mono text-slate-200 font-semibold">{r.symbol || '—'}</td>
                    <td className="px-3 py-2 text-slate-400 text-[12px]">{r.trigger?.replace(/^pre_trade_/, '')}</td>
                    <td className="px-3 py-2">
                      <span className={`inline-block text-[11px] font-bold px-2 py-0.5 rounded border ${style}`}>
                        {r.verdict || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-300 text-[12px]">{r.reason || '—'}</td>
                  </tr>
                  {isOpen && (
                    <tr className="border-b border-white/5 bg-black/30">
                      <td colSpan={5} className="px-4 py-3">
                        <pre className="text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap font-mono">
                          {r.analysis}
                        </pre>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
