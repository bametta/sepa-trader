/**
 * IRA Portfolio Manager tab — AI-guided long-term ETF allocation (Roth IRA).
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import { fetchIRAStatus, fetchIRATarget, fetchIRAHistory, runIRARebalance } from '../api/client'

const usd = v => v != null ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(v) : '—'
const pct = (v, d = 1) => v != null ? `${(v * 100).toFixed(d)}%` : '—'
const signed = v => v != null ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '—'

const REGIME = {
  risk_on:  { label: 'Risk-on',  cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25' },
  neutral:  { label: 'Neutral',  cls: 'text-amber-400 bg-amber-500/10 border-amber-500/25' },
  risk_off: { label: 'Risk-off', cls: 'text-red-400 bg-red-500/10 border-red-500/25' },
}

function errMsg(e) {
  return e?.response?.data?.detail || e?.message || 'Request failed'
}

function Stat({ label, value, sub }) {
  return (
    <div className="card-sm">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-lg font-bold text-slate-100 num mt-1">{value}</div>
      {sub && <div className="text-[10px] text-slate-600 mt-0.5">{sub}</div>}
    </div>
  )
}

function PlanTable({ plan }) {
  if (!plan?.length) return <div className="text-sm text-slate-500">No trades needed.</div>
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-[10px] uppercase tracking-wider text-slate-500 text-left">
          <th className="py-1">Side</th><th>ETF</th><th className="text-right">Amount</th>
          <th className="text-right">Drift</th><th className="text-right">Status</th>
        </tr>
      </thead>
      <tbody>
        {plan.map((o, i) => (
          <tr key={i} className="border-t border-white/[0.05]">
            <td className={`py-1.5 font-semibold ${o.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}`}>
              {o.side.toUpperCase()}
            </td>
            <td className="text-slate-200">{o.symbol}{o.full ? ' (exit)' : ''}</td>
            <td className="text-right num text-slate-200">
              {usd(o.notional)}{o.capped ? ' *' : ''}
            </td>
            <td className="text-right num text-slate-400">{signed(o.drift_pct)}</td>
            <td className="text-right text-slate-400">{o.status || 'proposed'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function IRAPortfolio() {
  const qc = useQueryClient()
  const [preview, setPreview] = useState(null)
  const [confirming, setConfirming] = useState(false)

  const status = useQuery('ira-status', fetchIRAStatus, { refetchInterval: 60000, retry: false })
  const connected = status.data?.connected
  const target = useQuery('ira-target', fetchIRATarget, { enabled: !!connected, retry: false, staleTime: 5 * 60000 })
  const history = useQuery('ira-history', () => fetchIRAHistory(10), { enabled: !!connected })

  const rebalance = useMutation(runIRARebalance, {
    onSuccess: (data, vars) => {
      setPreview(vars.execute ? null : data)
      setConfirming(false)
      qc.invalidateQueries('ira-status')
      qc.invalidateQueries('ira-history')
    },
  })

  if (status.isLoading) return <div className="text-slate-500 text-sm">Loading IRA…</div>
  if (status.isError) {
    return <div className="card text-red-400 text-sm">{errMsg(status.error)}</div>
  }
  if (!connected) {
    return (
      <div className="card space-y-2">
        <div className="text-slate-100 font-semibold">IRA Portfolio Manager</div>
        <div className="text-sm text-slate-400">
          Add your Alpaca IRA API keys in Settings → IRA Portfolio to connect. This account is
          kept fully separate from your trading accounts.
        </div>
      </div>
    )
  }

  const snap = status.data.snapshot
  const cfg = status.data.config
  const t = target.data
  const regime = t ? (REGIME[t.regime] || REGIME.neutral) : null

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="IRA equity" value={usd(snap.equity)} />
        <Stat label="Settled cash" value={usd(snap.spendable)} sub="spendable (cash account)" />
        <Stat label="Auto-rebalance" value={cfg.enabled ? 'On' : 'Off'}
              sub={`drift ≥ ${cfg.threshold_pct}%`} />
        <Stat label="Regime" value={regime ? regime.label : '…'} />
      </div>

      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-slate-100 font-semibold">Target vs current</div>
          {regime && (
            <span className={`text-xs px-2 py-0.5 rounded border ${regime.cls}`}>{regime.label}</span>
          )}
        </div>
        {target.isLoading && <div className="text-sm text-slate-500">Building AI target…</div>}
        {target.isError && <div className="text-sm text-red-400">{errMsg(target.error)}</div>}
        {t && (
          <>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-slate-500 text-left">
                  <th className="py-1">ETF</th>
                  <th className="text-right">Current</th><th className="text-right">Target</th>
                  <th className="text-right">Drift</th>
                </tr>
              </thead>
              <tbody>
                {t.rows.map(r => (
                  <tr key={r.symbol} className="border-t border-white/[0.05]">
                    <td className="py-1.5 text-slate-200 font-medium">{r.symbol}</td>
                    <td className="text-right num text-slate-300">{pct(r.current_weight)}</td>
                    <td className="text-right num text-slate-300">{pct(r.target_weight)}</td>
                    <td className={`text-right num ${Math.abs(r.drift_pct) >= cfg.threshold_pct ? 'text-amber-400' : 'text-slate-500'}`}>
                      {signed(r.drift_pct)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {t.rationale && (
              <div className="text-xs text-slate-400 italic border-t border-white/[0.05] pt-2">
                {t.rationale}
              </div>
            )}
          </>
        )}
      </div>

      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-slate-100 font-semibold">Rebalance</div>
          <div className="flex gap-2">
            <button className="btn-ghost" disabled={rebalance.isLoading}
                    onClick={() => rebalance.mutate({ execute: false, force: false })}>
              Preview
            </button>
            {preview?.plan?.length > 0 && !confirming && (
              <button className="btn-primary" onClick={() => setConfirming(true)}>Execute…</button>
            )}
            {confirming && (
              <button className="btn-primary" disabled={rebalance.isLoading}
                      onClick={() => rebalance.mutate({ execute: true, force: false })}>
                Confirm — place {preview.plan.length} order(s)
              </button>
            )}
          </div>
        </div>
        {rebalance.isError && <div className="text-sm text-red-400">{errMsg(rebalance.error)}</div>}
        {rebalance.isSuccess && rebalance.data?.executed && (
          <div className="text-sm text-emerald-400">Orders submitted.</div>
        )}
        {rebalance.data?.note && <div className="text-sm text-slate-400">{rebalance.data.note}</div>}
        {preview && <PlanTable plan={preview.plan} />}
        {preview?.plan?.some(o => o.capped) && (
          <div className="text-[11px] text-slate-500">
            * Buy reduced to settled cash. Sale proceeds settle T+1; the next run completes the buy.
          </div>
        )}
      </div>

      <div className="card space-y-2">
        <div className="text-slate-100 font-semibold">History</div>
        {(history.data || []).length === 0 && <div className="text-sm text-slate-500">No runs yet.</div>}
        {(history.data || []).map(h => (
          <div key={h.id} className="border-t border-white/[0.05] pt-2 text-sm">
            <div className="flex justify-between">
              <span className="text-slate-200">{h.ai_verdict} · {h.reasoning}</span>
              <span className="text-slate-500 text-xs">{new Date(h.created_at).toLocaleString()}</span>
            </div>
            {h.ai_reasoning && <div className="text-xs text-slate-500 mt-0.5">{h.ai_reasoning}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}
