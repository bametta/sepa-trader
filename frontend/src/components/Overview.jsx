/**
 * Overview: one-screen snapshot of everything the app tracks. Read-only; every panel reuses an existing endpoint
 * and fails independently, so one slow or broken source never blanks the page.
 */
import { useQuery } from 'react-query'
import {
  fetchSavingsSummary, fetchSavingsPlan, fetchSavingsHistory, fetchAccountsOverview, fetchPositions,
  fetchIRAStatus, fetchDMPosition, fetchDMSignal, fetchMarketEnvironment, fetchSettings,
} from '../api/client'

const usd0 = v => (v != null ? `$${Math.round(v).toLocaleString('en-US')}` : '—')
const usdS = v => (v != null ? `${v >= 0 ? '+' : '-'}$${Math.abs(Math.round(v)).toLocaleString('en-US')}` : '—')
const pct = (v, d = 0) => (v != null ? `${(v * 100).toFixed(d)}%` : '—')
const TAX_LABEL = { pre_tax: 'Pre-tax', roth: 'Roth', taxable: 'Taxable', hsa: 'HSA', other: 'Other' }
const opts = (ms = 60000) => ({ staleTime: ms, retry: false, refetchOnWindowFocus: false })

function Panel({ title, to, onNavigate, children, error, loading }) {
  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-bold uppercase tracking-widest text-slate-500">{title}</div>
        {to && (
          <button onClick={() => onNavigate(to)} className="text-[11px] text-indigo-300 hover:text-indigo-200">
            Open →
          </button>
        )}
      </div>
      {loading ? <div className="h-16 rounded bg-white/[0.04] animate-pulse" />
        : error ? <div className="text-xs text-slate-500">Not available right now.</div>
        : children}
    </div>
  )
}

function Stat({ label, value, sub, tone }) {
  const color = tone === 'good' ? 'text-emerald-400' : tone === 'bad' ? 'text-red-400' : 'text-slate-100'
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`num text-lg font-semibold ${color}`}>{value}</div>
      {sub && <div className="text-[11px] text-slate-500">{sub}</div>}
    </div>
  )
}

function Spark({ points }) {
  if (!points || points.length < 2) return <div className="text-xs text-slate-500">Trend appears after a few days of history.</div>
  const vals = points.map(p => p.total)
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1
  const w = 600, h = 70
  const d = points.map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p.total - lo) / span) * (h - 8) - 4}`).join(' ')
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-16" preserveAspectRatio="none" role="img"
         aria-label="Total savings over time">
      <polyline points={d} fill="none" stroke="rgb(129 140 248)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

function Bar({ label, value, total }) {
  return (
    <div className="text-xs">
      <div className="flex justify-between text-slate-300">
        <span>{label}</span><span className="num text-slate-400">{usd0(value)} · {pct(total ? value / total : 0)}</span>
      </div>
      <div className="h-1.5 rounded bg-white/[0.06] overflow-hidden">
        <div className="h-full bg-indigo-400/70" style={{ width: `${total ? Math.min(100, (value / total) * 100) : 0}%` }} />
      </div>
    </div>
  )
}

export default function Overview({ onNavigate }) {
  const summary = useQuery('ov-savings', fetchSavingsSummary, opts(120000))
  const plan = useQuery('ov-plan', fetchSavingsPlan, opts(300000))
  const hist = useQuery('ov-hist', fetchSavingsHistory, opts(300000))
  const accts = useQuery('ov-accounts', fetchAccountsOverview, opts(30000))
  const positions = useQuery('positions', fetchPositions, opts(10000))
  const ira = useQuery('ov-ira', fetchIRAStatus, opts(60000))
  const dmPos = useQuery('ov-dm-pos', fetchDMPosition, opts(60000))
  const dmSig = useQuery('ov-dm-sig', fetchDMSignal, opts(60000))
  const market = useQuery('ov-market', fetchMarketEnvironment, opts(300000))
  const settings = useQuery('settings', fetchSettings, opts(30000))

  const s = summary.data
  const p = plan.data
  const mode = settings.data?.trading_mode || 'paper'
  const modeAccts = (accts.data?.[mode] || [])
  const pts = Array.isArray(hist.data) ? hist.data : (hist.data?.points || [])
  const last = pts[pts.length - 1], prev = pts[pts.length - 2]
  const change = last && prev ? last.total - prev.total : null

  // "Needs attention": anything you should look at, collected from every source
  const attention = []
  ;(s?.warnings || []).forEach(w => attention.push({ text: w.detail, to: 'Savings' }))
  const bad = (positions.data || []).filter(x => x.signal === 'NO_SETUP')
  if (bad.length) attention.push({ text: `Stage 2 lost: ${bad.map(x => x.symbol).join(', ')}`, to: 'Positions', hot: true })
  if (ira.data?.connected && ira.data.config?.halted) attention.push({ text: 'IRA trading is halted (kill switch on).', to: 'IRA', hot: true })
  if (p?.ready && p.retirement_income?.funded_ratio != null && p.retirement_income.funded_ratio < 0.8) {
    attention.push({ text: `Retirement plan is ${pct(p.retirement_income.funded_ratio)} funded in the median case.`, to: 'Savings' })
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 card p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-[10px] font-bold uppercase tracking-widest text-slate-500">Everything you track</div>
            <button onClick={() => onNavigate('Savings')} className="text-[11px] text-indigo-300 hover:text-indigo-200">Open →</button>
          </div>
          {summary.isLoading ? <div className="h-24 rounded bg-white/[0.04] animate-pulse" />
            : summary.isError ? <div className="text-xs text-slate-500">Savings not available right now.</div>
            : (
              <>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                  <Stat label="Total" value={usd0(s.total)} sub={change != null ? `${usdS(change)} since last snapshot` : null}
                        tone={change > 0 ? 'good' : change < 0 ? 'bad' : undefined} />
                  <Stat label="Retirement" value={usd0(s.retirement_total)} />
                  <Stat label="Stocks" value={pct(s.equity_share)} sub={p?.ready ? `glidepath ${pct(p.glidepath_equity)}` : null} />
                  <Stat label="Tech funds" value={pct(s.by_theme?.tech_pct)} sub="limit 30%"
                        tone={s.by_theme?.tech_pct > 0.3 ? 'bad' : undefined} />
                </div>
                <Spark points={pts} />
              </>
            )}
        </div>

        <Panel title="Needs attention" loading={summary.isLoading && !attention.length}>
          {attention.length === 0
            ? <div className="text-xs text-emerald-400">Nothing needs attention.</div>
            : (
              <ul className="space-y-2">
                {attention.slice(0, 6).map((a, i) => (
                  <li key={i} className="text-xs flex gap-2">
                    <span className={a.hot ? 'text-red-400' : 'text-amber-400'}>●</span>
                    <button className="text-left text-slate-300 hover:text-slate-100" onClick={() => onNavigate(a.to)}>{a.text}</button>
                  </li>
                ))}
                {attention.length > 6 && <li className="text-[11px] text-slate-500">+{attention.length - 6} more in Savings</li>}
              </ul>
            )}
        </Panel>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Panel title="Retirement plan" to="Savings" onNavigate={onNavigate} loading={plan.isLoading} error={plan.isError}>
          {!p?.ready ? <div className="text-xs text-slate-500">{p?.note || 'Set your age and retirement age in Settings.'}</div> : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label={`At age ${p.retire_age} (median)`} value={usd0(p.p50)} sub={`range ${usd0(p.p10)} – ${usd0(p.p90)}`} />
              <Stat label="Years to go" value={p.years} sub={`saving ${usd0(p.annual_contribution)}/yr`} />
              {p.target ? <Stat label="Goal" value={usd0(p.target)}
                               sub={p.retirement_income?.funded_ratio != null ? `${pct(p.retirement_income.funded_ratio)} funded` : null}
                               tone={p.retirement_income?.funded_ratio >= 1 ? 'good' : undefined} /> : null}
              {p.income_at_safe_rate ? <Stat label="Portfolio income" value={`${usd0(p.income_at_safe_rate.p50)}/yr`} sub="median, safe rate" /> : null}
            </div>
          )}
        </Panel>

        <Panel title="By account" to="Savings" onNavigate={onNavigate} loading={summary.isLoading} error={summary.isError}>
          <div className="space-y-2">
            {(s?.accounts || []).slice().sort((a, b) => b.value - a.value).slice(0, 7).map(a => (
              <Bar key={a.id} label={`${a.provider} · ${a.name}`} value={a.value} total={s.total} />
            ))}
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-[11px] text-slate-500">
              {Object.entries(s?.by_tax || {}).map(([k, v]) => <span key={k}>{TAX_LABEL[k] || k} {usd0(v)}</span>)}
            </div>
          </div>
        </Panel>

        <Panel title="Biggest positions" to="Savings" onNavigate={onNavigate} loading={summary.isLoading} error={summary.isError}>
          <div className="space-y-2">
            {(s?.positions || []).slice(0, 7).map(x => (
              <Bar key={x.name} label={x.symbol || x.name} value={x.value} total={s.total} />
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <Panel title={`Trading · ${mode}`} to="Positions" onNavigate={onNavigate} loading={accts.isLoading} error={accts.isError}>
          {modeAccts.length === 0 ? <div className="text-xs text-slate-500">No Alpaca account connected for {mode}.</div> : (
            <div className="space-y-3">
              {modeAccts.slice(0, 3).map(a => (
                <Stat key={a.name} label={a.name} value={usd0(a.portfolio_value)}
                      sub={`today ${usdS(a.day_pnl)}`} tone={a.day_pnl > 0 ? 'good' : a.day_pnl < 0 ? 'bad' : undefined} />
              ))}
              <div className="text-[11px] text-slate-500">
                {(positions.data || []).length} open position(s)
                {(positions.data || []).some(x => x.signal === 'BREAKOUT') ? ' · breakout active' : ''}
              </div>
            </div>
          )}
        </Panel>

        <Panel title="IRA manager" to="IRA" onNavigate={onNavigate} loading={ira.isLoading} error={ira.isError}>
          {!ira.data?.connected ? <div className="text-xs text-slate-500">Not connected.</div> : (
            <div className="space-y-2">
              <Stat label="Equity" value={usd0(ira.data.snapshot?.equity)} sub={`cash ${usd0(ira.data.snapshot?.cash)}`} />
              <div className="text-[11px]">
                <span className={ira.data.config?.halted ? 'text-red-400' : ira.data.config?.enabled ? 'text-emerald-400' : 'text-slate-500'}>
                  {ira.data.config?.halted ? 'HALTED' : ira.data.config?.enabled ? 'Auto-rebalance on' : 'Auto-rebalance off'}
                </span>
                <span className="text-slate-500"> · {(ira.data.snapshot?.holdings || []).length} fund(s)</span>
              </div>
            </div>
          )}
        </Panel>

        <Panel title="Dual momentum" to="Dual Momentum" onNavigate={onNavigate} loading={dmPos.isLoading && dmSig.isLoading}
               error={dmPos.isError && dmSig.isError}>
          <div className="space-y-2 text-xs text-slate-300">
            {Array.isArray(dmPos.data) && dmPos.data.length
              ? dmPos.data.map(x => <div key={x.symbol}>Holding <b>{x.symbol}</b> · {usd0(x.market_value)}</div>)
              : <div className="text-slate-500">No position.</div>}
            {dmSig.data?.recommended_symbol
              ? <div className="text-slate-400">Latest signal: {dmSig.data.action} {dmSig.data.recommended_symbol}</div>
              : null}
          </div>
        </Panel>

        <Panel title="Market" loading={market.isLoading} error={market.isError}>
          <div className="space-y-2">
            <Stat label="Regime" value={market.data?.environment || '—'}
                  tone={market.data?.environment === 'BULL' ? 'good' : market.data?.environment === 'BEAR' ? 'bad' : undefined} />
            <div className="text-[11px] text-slate-500">
              SPY {market.data?.spy_above_200 ? 'above' : 'below'} 200-day · VIX {market.data?.vix ?? '—'}
            </div>
          </div>
        </Panel>
      </div>

      <div className="text-[11px] text-slate-600">
        Read-only snapshot. Savings values come from your imports and manual updates; planning aid only, not financial advice.
      </div>
    </div>
  )
}
