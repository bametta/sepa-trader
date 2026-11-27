/**
 * IRA Portfolio Manager tab — AI-guided long-term ETF allocation (Roth IRA).
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import {
  fetchIRAStatus, fetchIRATarget, fetchIRAHistory, runIRARebalance,
  fetchIRAPlan, runIRABacktest, setIRAHalt,
} from '../api/client'

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
            <td className="text-right text-slate-400">{o.final_status || o.status || 'proposed'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

const VERDICT = {
  APPROVE: 'text-emerald-400', NO_TRADES: 'text-slate-400',
  BLOCK: 'text-red-400', UNAVAILABLE: 'text-amber-400',
}
const SEV = { block: 'text-red-400', warn: 'text-amber-400', info: 'text-slate-500' }
const usd0 = v => v != null ? `$${Math.round(v).toLocaleString('en-US')}` : '—'

function GateCard({ gate }) {
  return (
    <div className="border-t border-white/[0.05] pt-3 space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider text-slate-500">AI gate</span>
        <span className={`text-sm font-semibold ${VERDICT[gate.verdict] || 'text-slate-300'}`}>{gate.verdict}</span>
        <span className="text-[11px] text-slate-500 num">
          expense {gate.metrics.blended_expense_pct}% · turnover {gate.metrics.turnover_pct}%
        </span>
      </div>
      {gate.reason && <div className="text-xs text-slate-400">{gate.reason}</div>}
      {gate.checks.map((c, i) => (
        <div key={i} className={`text-[11px] ${SEV[c.severity]}`}>{c.severity.toUpperCase()} · {c.detail}</div>
      ))}
    </div>
  )
}

function PlanCard({ plan }) {
  if (!plan) return null
  if (!plan.ready) {
    return (
      <div className="card space-y-1">
        <div className="text-slate-100 font-semibold">Retirement plan</div>
        <div className="text-sm text-slate-400">
          Add your birth year and a retirement target (or yearly income goal) in Settings → IRA Portfolio
          to get a projection and contribution recommendation.
        </div>
      </div>
    )
  }
  const r = plan.recommendation
  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-slate-100 font-semibold">Retirement plan</div>
        <span className="text-[11px] text-slate-500 num">
          age {plan.profile.age} → {plan.profile.retire_age} · glidepath equity {pct(plan.glidepath_equity, 0)}
          {plan.glidepath_gap != null && ` (target ${plan.glidepath_gap >= 0 ? '+' : ''}${(plan.glidepath_gap * 100).toFixed(0)} pts)`}
        </span>
      </div>
      {plan.target?.amount && (
        <div className="text-sm text-slate-300">
          Goal {usd0(plan.target.amount)} in today's dollars ({plan.target.basis}) · expected real return {pct(plan.expected_real_return)}, volatility {pct(plan.expected_volatility)}
        </div>
      )}
      {r && (
        <div className="rounded-lg border border-white/[0.08] p-3 space-y-1">
          <div className="text-[10px] uppercase tracking-wider text-slate-500">Recommended contribution</div>
          <div className="text-lg font-bold text-slate-100 num">
            {r.kind === 'already_on_track' ? 'Already on track'
              : `${usd0(r.annual)}/yr · ${usd0(r.monthly)}/mo (${pct(r.pct_of_limit, 0)} of the ${usd0(plan.contribution_limit)} limit)`}
            {r.pct_of_income != null && r.kind !== 'already_on_track' && ` · ${pct(r.pct_of_income)} of income`}
          </div>
          {r.kind === 'limit_not_enough' && (
            <div className="text-xs text-amber-400">
              Maxing the IRA gives only a {pct(r.prob_at_limit, 0)} chance. About {usd0(r.total_annual_saving_needed)}/yr
              total saving is needed for {pct(r.success_threshold, 0)} confidence, i.e. {usd0(r.extra_outside_ira)}/yr beyond the IRA.
            </div>
          )}
        </div>
      )}
      <table className="w-full text-sm">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-slate-500 text-left">
            <th className="py-1">Contribution</th><th className="text-right">Poor (p10)</th>
            <th className="text-right">Median</th><th className="text-right">Good (p90)</th>
            {plan.target?.amount && <th className="text-right">Reach goal</th>}
          </tr>
        </thead>
        <tbody>
          {plan.scenarios.map(s => (
            <tr key={s.annual} className="border-t border-white/[0.05]">
              <td className="py-1.5 text-slate-200 num">{usd0(s.annual)}/yr ({pct(s.pct_of_limit, 0)})</td>
              <td className="text-right num text-slate-400">{usd0(s.p10)}</td>
              <td className="text-right num text-slate-200">{usd0(s.p50)}</td>
              <td className="text-right num text-slate-400">{usd0(s.p90)}</td>
              {plan.target?.amount && <td className="text-right num text-slate-300">{pct(s.prob_reach_target, 0)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-[11px] text-slate-600">
        {plan.disclaimer} IRS limits: {plan.limits.note}
      </div>
    </div>
  )
}

function BacktestCard() {
  const bt = useMutation(runIRABacktest)
  const res = bt.data?.results
  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-slate-100 font-semibold">Strategy backtest</div>
        <button className="btn-ghost" disabled={bt.isLoading}
                onClick={() => bt.mutate({ cadence: 'weekly', threshold: 0.05, cost_bps: 3 })}>
          {bt.isLoading ? 'Running…' : 'Run backtest'}
        </button>
      </div>
      {bt.isError && <div className="text-sm text-red-400">{errMsg(bt.error)}</div>}
      {res && (
        <>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-slate-500 text-left">
                <th className="py-1">Strategy</th><th className="text-right">CAGR</th><th className="text-right">Vol</th>
                <th className="text-right">Sharpe</th><th className="text-right">Max DD</th><th className="text-right">Turnover/yr</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(res).map(([name, m]) => (
                <tr key={name} className="border-t border-white/[0.05]">
                  <td className="py-1.5 text-slate-200">{name.replace('_', ' ')}</td>
                  <td className="text-right num text-slate-200">{pct(m.cagr)}</td>
                  <td className="text-right num text-slate-400">{pct(m.volatility)}</td>
                  <td className="text-right num text-slate-300">{m.sharpe ?? '—'}</td>
                  <td className="text-right num text-red-400">{pct(m.max_drawdown)}</td>
                  <td className="text-right num text-slate-400">{m.turnover_per_year.toFixed(2)}x</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-[11px] text-slate-500">
            {bt.data.data_mode} · {bt.data.period.start} → {bt.data.period.end}
          </div>

          <div className="border-t border-white/[0.05] pt-3 space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">
              Does timing beat a static portfolio with the same average holdings?
            </div>
            {Object.entries(bt.data.verdict || {}).map(([name, v]) => (
              <div key={name} className="text-sm">
                <span className={`font-semibold ${v.earns_its_place ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {name.replace('_', ' ')}: {v.earns_its_place ? 'earns its place' : 'does not clear the bar'}
                </span>
                <span className="text-slate-400">
                  {' '}· max drawdown {pct(v.drawdown_vs_matched_static)} vs static · CAGR {pct(v.cagr_vs_matched_static)}
                  {' '}· shallower in {v.stress_windows_with_shallower_drawdown} crashes
                  {' '}· holds in {pct(v.sensitivity_pass_rate, 0)} of parameter settings
                </span>
              </div>
            ))}
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-slate-500 text-left">
                <th className="py-1">Crash</th><th>Strategy</th><th className="text-right">Max DD</th>
                <th className="text-right">Static, same holdings</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(bt.data.stress || {}).flatMap(([label, row]) =>
                ['regime_baseline', 'regime_trend'].map(n => (
                  <tr key={label + n} className="border-t border-white/[0.05]">
                    <td className="py-1 text-slate-300">{label.replace(/_/g, ' ')}</td>
                    <td className="text-slate-400">{n.replace('regime_', '')}</td>
                    <td className="text-right num text-slate-200">{pct(row[n]?.max_drawdown)}</td>
                    <td className="text-right num text-slate-400">{pct(row['matched_static_for_' + n]?.max_drawdown)}</td>
                  </tr>
                )))}
            </tbody>
          </table>

          {bt.data.rolling && (
            <div className="text-xs text-slate-400">
              Rolling 5-year windows, trend strategy vs static: smaller drawdown in {pct(bt.data.rolling['5y']?.regime_trend?.shallower_drawdown, 0)},
              higher return in {pct(bt.data.rolling['5y']?.regime_trend?.beat_on_return, 0)}. The benefit is crash protection and it usually costs return in calm markets.
            </div>
          )}
          <div className="text-[11px] text-slate-600">{bt.data.notes.join(' ')}</div>
        </>
      )}
    </div>
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
  const plan = useQuery('ira-plan', fetchIRAPlan, { enabled: !!connected, retry: false, staleTime: 5 * 60000 })

  const rebalance = useMutation(runIRARebalance, {
    onSuccess: (data) => {
      setPreview(data)
      setConfirming(false)
      qc.invalidateQueries('ira-status')
      qc.invalidateQueries('ira-history')
    },
  })

  const halt = useMutation(setIRAHalt, { onSuccess: () => qc.invalidateQueries('ira-status') })
  const canExecute = !!preview?.proposal_id && !preview?.executed && !preview?.final_status

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
      <div className={`card flex items-center justify-between ${cfg.halted ? 'border border-red-500/40' : ''}`}>
        <div className="text-sm">
          <span className={`font-semibold ${cfg.halted ? 'text-red-400' : 'text-emerald-400'}`}>
            {cfg.halted ? 'Trading HALTED' : 'Trading active'}
          </span>
          <span className="text-slate-500"> · kill switch blocks all IRA orders, manual and scheduled · AI tilt ±{cfg.tilt_pp} pts</span>
        </div>
        <button className={cfg.halted ? 'btn-primary' : 'btn-ghost'} disabled={halt.isLoading}
                onClick={() => halt.mutate({ halted: !cfg.halted })}>
          {cfg.halted ? 'Resume trading' : 'Halt trading'}
        </button>
      </div>

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
            {canExecute && !confirming && (
              <button className="btn-primary" onClick={() => setConfirming(true)}>Execute…</button>
            )}
            {canExecute && confirming && (
              <button className="btn-primary" disabled={rebalance.isLoading}
                      onClick={() => rebalance.mutate({ execute: true, proposal_id: preview.proposal_id })}>
                Confirm — place exactly these {preview.plan.length} order(s)
              </button>
            )}
          </div>
        </div>
        {rebalance.isError && <div className="text-sm text-red-400">{errMsg(rebalance.error)}</div>}
        {rebalance.data?.final_status && (
          <div className={`text-sm ${rebalance.data.final_status === 'EXECUTED' ? 'text-emerald-400' : 'text-amber-400'}`}>
            {rebalance.data.final_status === 'EXECUTED' ? 'All orders filled.'
              : `Execution ${rebalance.data.final_status}: ${(rebalance.data.problems || []).join('; ') || 'check Alpaca'}`}
          </div>
        )}
        {preview && !preview.proposal_id && preview.plan?.length > 0 && !preview.executed && !preview.final_status && (
          <div className="text-sm text-amber-400">
            Not executable: {preview.halted ? 'trading is halted.' : `AI gate ${preview.gate?.verdict}.`} {preview.gate?.reason}
          </div>
        )}
        {rebalance.data?.note && <div className="text-sm text-slate-400">{rebalance.data.note}</div>}
        {preview && <PlanTable plan={preview.results?.length ? preview.results : preview.plan} />}
        {preview?.gate && <GateCard gate={preview.gate} />}
        {preview?.plan?.some(o => o.capped) && (
          <div className="text-[11px] text-slate-500">
            * Buy reduced to settled cash. Sale proceeds settle T+1; the next run completes the buy.
          </div>
        )}
      </div>

      <PlanCard plan={plan.data} />
      <BacktestCard />

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
