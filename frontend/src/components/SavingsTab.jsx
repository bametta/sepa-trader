/**
 * Savings tab: every account across providers in one place (manual-first, no credentials stored).
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import {
  fetchSavingsSummary, fetchSavingsPlan, fetchSavingsHistory, fetchSavingsChanges, createSavingsAccount, patchSavingsAccount,
  deleteSavingsAccount, fetchHoldingsCsv, saveHoldingsCsv,
} from '../api/client'

const usd0 = v => v != null ? `$${Math.round(v).toLocaleString('en-US')}` : '—'
const pct = (v, d = 0) => v != null ? `${(v * 100).toFixed(d)}%` : '—'
const errMsg = e => e?.response?.data?.detail || e?.message || 'Request failed'

const TYPES = [
  ['401k', '401(k) / 403(b), traditional'], ['roth_401k', 'Roth 401(k)'], ['ira', 'Traditional IRA'],
  ['roth_ira', 'Roth IRA'], ['sdira', 'Self-directed IRA'], ['roth_sdira', 'Self-directed Roth IRA'],
  ['hsa', 'HSA'], ['taxable', 'Taxable brokerage'], ['other', 'Other'],
]
const TYPE_LABEL = Object.fromEntries(TYPES)
const TAX_LABEL = { pre_tax: 'Pre-tax', roth: 'Roth', taxable: 'Taxable', hsa: 'HSA', other: 'Other' }
const CLASS_LABEL = {
  us_large: 'US large cap', us_midsmall: 'US mid/small', intl_dev: 'Intl developed', intl_em: 'Emerging markets',
  reit: 'REITs', bonds_agg: 'Bonds', bonds_tips: 'TIPS', gold: 'Gold', cash: 'Cash', company_stock: 'Employer stock',
  crypto: 'Crypto', alt_real_estate: 'Real estate (alt)', alt_private: 'Private (alt)', alt_other: 'Other alt',
  other: 'Other', unclassified: 'Unclassified',
}
const SEV = { warn: 'text-amber-400', info: 'text-slate-400' }

function Bars({ title, data, total, labels = {} }) {
  const rows = Object.entries(data || {}).filter(([, v]) => v > 0)
  if (!rows.length) return null
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{title}</div>
      {rows.map(([k, v]) => (
        <div key={k} className="text-xs">
          <div className="flex justify-between text-slate-300">
            <span>{labels[k] || k}</span><span className="num text-slate-400">{usd0(v)} · {pct(total ? v / total : 0)}</span>
          </div>
          <div className="h-1.5 rounded bg-white/[0.06] overflow-hidden">
            <div className="h-full bg-indigo-400/70" style={{ width: `${total ? Math.min(100, (v / total) * 100) : 0}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

function Sparkline({ points }) {
  if (!points || points.length < 2) {
    return <div className="text-xs text-slate-500">History builds up as you open this tab each day.</div>
  }
  const vals = points.map(p => p.total)
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1
  const w = 600, h = 80
  const d = points.map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p.total - lo) / span) * (h - 8) - 4}`).join(' ')
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-20" preserveAspectRatio="none">
        <polyline points={d} fill="none" stroke="currentColor" strokeWidth="2" className="text-indigo-400" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-[11px] text-slate-500">
        <span>{points[0].as_of} · {usd0(vals[0])}</span><span>{points[points.length - 1].as_of} · {usd0(vals[vals.length - 1])}</span>
      </div>
    </div>
  )
}

function AccountCard({ a, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [csv, setCsv] = useState('')
  const [contrib, setContrib] = useState(a.annual_contribution)
  const load = useMutation(fetchHoldingsCsv, { onSuccess: d => { setCsv(d.csv); setEditing(true) } })
  const save = useMutation(saveHoldingsCsv, { onSuccess: () => { setEditing(false); onChanged() } })
  const patch = useMutation(patchSavingsAccount, { onSuccess: onChanged })
  const del = useMutation(deleteSavingsAccount, { onSuccess: onChanged })

  return (
    <div className="rounded-lg border border-white/[0.08] p-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-slate-100 font-semibold">{a.provider} <span className="text-slate-500 font-normal">· {a.name}</span></div>
          <div className="text-[11px] text-slate-500">
            {TYPE_LABEL[a.account_type] || a.account_type} · {TAX_LABEL[a.tax]}
            {a.contribution_source === 'employer' && ' · employer-funded'}{!a.retirement && ' · not counted for retirement'}
          </div>
        </div>
        <div className="text-right">
          <div className="text-lg font-bold text-slate-100 num">{usd0(a.value)}</div>
          {!a.read_only && (
            <div className="text-[11px] text-slate-500 flex items-center gap-1 justify-end">
              saves
              <input className="input w-20 text-right py-0" type="number" min="0" value={contrib}
                     onChange={e => setContrib(e.target.value)}
                     onBlur={() => parseFloat(contrib) !== a.annual_contribution && patch.mutate({ id: a.id, annual_contribution: parseFloat(contrib) || 0 })} />
              /yr
            </div>
          )}
          {a.read_only && <div className="text-[11px] text-slate-500">managed in the IRA tab</div>}
        </div>
      </div>

      {a.holdings.length > 0 && !editing && (
        <table className="w-full text-xs">
          <tbody>
            {a.holdings.map((h, i) => (
              <tr key={(h.id ?? 'x') + i} className="border-t border-white/[0.05]">
                <td className="py-1 text-slate-300">{h.symbol ? `${h.symbol} ` : ''}<span className="text-slate-500">{h.symbol && h.name !== h.symbol ? h.name : (!h.symbol ? h.name : '')}</span></td>
                <td className="text-slate-500">{h.source === 'live' ? 'live price' : h.source === 'manual' ? `as of ${h.as_of}` : 'no value'}</td>
                <td className={`text-right num ${h.stale || h.source === 'missing' ? 'text-amber-400' : 'text-slate-200'}`}>{usd0(h.value)}</td>
                <td className="text-right num text-slate-500 w-14">{pct(h.pct_of_account)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {a.holdings.length === 0 && !a.read_only && !editing && (
        <div className="text-xs text-slate-500">No holdings yet. Use "Edit holdings" to paste them.</div>
      )}

      {editing && (
        <div className="space-y-2">
          <div className="text-[11px] text-slate-500">
            One row per holding. Columns: symbol, name, quantity, value, asset_class, illiquid, as_of. Use quantity for tickers
            (priced live) or value for core funds, real estate and notes. asset_class can stay "auto" for known ETFs.
          </div>
          <textarea className="input w-full font-mono text-xs" rows={Math.min(14, Math.max(5, csv.split('\n').length + 1))}
                    value={csv} onChange={e => setCsv(e.target.value)} spellCheck={false} />
          <div className="flex gap-2">
            <button className="btn-primary" disabled={save.isLoading} onClick={() => save.mutate({ id: a.id, csv })}>
              {save.isLoading ? 'Saving…' : 'Save holdings'}
            </button>
            <button className="btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
          {save.isError && <div className="text-sm text-red-400">{errMsg(save.error)}</div>}
        </div>
      )}

      {!a.read_only && !editing && (
        <div className="flex gap-3 text-xs">
          <button className="underline text-slate-400 hover:text-slate-200" disabled={load.isLoading}
                  onClick={() => load.mutate(a.id)}>Edit holdings</button>
          <button className="underline text-slate-600 hover:text-red-400"
                  onClick={() => window.confirm(`Delete ${a.provider} ${a.name} and all its holdings?`) && del.mutate(a.id)}>
            Delete account
          </button>
        </div>
      )}
      {(load.isError || patch.isError || del.isError) && (
        <div className="text-sm text-red-400">{errMsg(load.error || patch.error || del.error)}</div>
      )}
    </div>
  )
}

function AddAccount({ onAdded }) {
  const blank = { provider: '', name: '', account_type: '401k', annual_contribution: '', contribution_source: 'employee', retirement: true }
  const [f, setF] = useState(blank)
  const add = useMutation(createSavingsAccount, { onSuccess: () => { setF(blank); onAdded() } })
  return (
    <form className="grid grid-cols-2 md:grid-cols-6 gap-2 items-end"
          onSubmit={e => { e.preventDefault(); add.mutate({ ...f, annual_contribution: parseFloat(f.annual_contribution) || 0 }) }}>
      <label className="text-[10px] uppercase tracking-wider text-slate-500">Provider
        <input className="input w-full" required maxLength={60} placeholder="Principal, Fidelity…" value={f.provider}
               onChange={e => setF({ ...f, provider: e.target.value })} />
      </label>
      <label className="text-[10px] uppercase tracking-wider text-slate-500">Account name
        <input className="input w-full" required maxLength={100} placeholder="Employer 401(k)" value={f.name}
               onChange={e => setF({ ...f, name: e.target.value })} />
      </label>
      <label className="text-[10px] uppercase tracking-wider text-slate-500">Type
        <select className="input w-full" value={f.account_type} onChange={e => setF({ ...f, account_type: e.target.value })}>
          {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      <label className="text-[10px] uppercase tracking-wider text-slate-500">Funded by
        <select className="input w-full" value={f.contribution_source}
                onChange={e => setF({ ...f, contribution_source: e.target.value })}>
          <option value="employee">Me (my paycheck)</option>
          <option value="employer">Employer (match)</option>
        </select>
      </label>
      <label className="text-[10px] uppercase tracking-wider text-slate-500">Goes in per year $
        <input className="input w-full" type="number" min="0" placeholder="this account only" value={f.annual_contribution}
               onChange={e => setF({ ...f, annual_contribution: e.target.value })} />
      </label>
      <label className="text-[10px] uppercase tracking-wider text-slate-500 flex items-center gap-2 pb-2">
        <input type="checkbox" checked={f.retirement} onChange={e => setF({ ...f, retirement: e.target.checked })} />
        For retirement
      </label>
      <button className="btn-primary" disabled={add.isLoading}>{add.isLoading ? 'Adding…' : 'Add account'}</button>
      {add.isError && <div className="col-span-full text-sm text-red-400">{errMsg(add.error)}</div>}
    </form>
  )
}

const EVENT_TEXT = {
  bought: e => `bought ${Math.abs(e.quantity_change)} ${e.name} (${usd0(e.value)})`,
  sold: e => `sold ${Math.abs(e.quantity_change)} ${e.name} (${usd0(e.value)})`,
  added: e => e.quantity != null
    ? `new position: ${e.quantity} ${e.name} (${usd0(e.value)})`
    : `new holding ${e.name} (${usd0(e.value)})`,
  removed: e => `${e.name} no longer held (was ${usd0(e.value)})`,
  value_update: e => `${e.name} value ${usd0(e.from)} → ${usd0(e.to)} (${e.change >= 0 ? '+' : ''}${usd0(e.change)})`,
}

function ChangesCard() {
  const q = useQuery('savings-changes', fetchSavingsChanges, { retry: false })
  const rows = q.data || []
  return (
    <div className="card space-y-2">
      <div className="text-slate-100 font-semibold">Recent activity</div>
      {q.isError && <div className="text-sm text-red-400">{errMsg(q.error)}</div>}
      {!q.isError && rows.length === 0 && (
        <div className="text-xs text-slate-500">
          No changes detected yet. Each day you open this tab (or a sync runs) is saved as a snapshot, and differences
          between snapshots show up here as buys, sells, new holdings and value updates.
        </div>
      )}
      {rows.slice(0, 15).map((c, i) => (
        <div key={i} className="border-t border-white/[0.05] pt-2 text-xs">
          <div className="flex justify-between text-slate-300">
            <span className="font-medium">{c.account}</span>
            <span className="text-slate-500">{c.from} → {c.to}</span>
          </div>
          {c.events.map((e, j) => <div key={j} className="text-slate-400">• {(EVENT_TEXT[e.type] || (() => e.type))(e)}</div>)}
          <div className="text-slate-600">
            account {c.total_change >= 0 ? '+' : ''}{usd0(c.total_change)}: trades {usd0(c.traded_net)}, market and other {usd0(c.market_and_other)}
          </div>
        </div>
      ))}
    </div>
  )
}

function PlanCard() {
  const plan = useQuery('savings-plan', fetchSavingsPlan, { retry: false, staleTime: 5 * 60000 })
  const p = plan.data
  if (plan.isLoading) return <div className="card text-sm text-slate-500">Projecting…</div>
  if (!p) return null
  if (!p.ready) return <div className="card text-sm text-slate-400">{p.note}</div>
  const inc = p.income_at_safe_rate
  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-slate-100 font-semibold">Combined retirement projection</div>
        <span className="text-[11px] text-slate-500 num">{p.years} years · glidepath equity {pct(p.glidepath_equity)} · you hold {pct(p.actual_equity_share)}</span>
      </div>
      <div className="text-sm text-slate-300">
        Starting from {usd0(p.balance)} and saving {usd0(p.annual_contribution)}/yr across your retirement accounts, in today's dollars:
        poor case {usd0(p.p10)}, median <span className="font-semibold text-slate-100">{usd0(p.p50)}</span>, good case {usd0(p.p90)}.
      </div>
      {inc && (
        <div className="text-xs text-slate-400">
          At the {pct(p.safe_withdrawal_rate, 1)} withdrawal rate that works 90% of the time, that is about {usd0(inc.p50)}/yr
          (range {usd0(inc.p10)} to {usd0(inc.p90)}).
        </div>
      )}
      {p.target
        ? <div className="text-sm text-slate-300">
            Goal {usd0(p.target)}: {pct(p.prob_reach_target)} chance of reaching it.
            {p.extra_saving_for_80pct != null && <span className="text-amber-400"> About {usd0(p.extra_saving_for_80pct)}/yr more saving gets you to 80%.</span>}
          </div>
        : <div className="text-xs text-slate-500">Set a total retirement goal in Settings → IRA Portfolio to see your odds.</div>}
      <div className="text-[11px] text-slate-600">{p.assumptions} Planning aid, not financial advice.</div>
    </div>
  )
}

export default function SavingsTab() {
  const qc = useQueryClient()
  const q = useQuery('savings-summary', fetchSavingsSummary, { retry: false, staleTime: 60000 })
  const hist = useQuery('savings-history', fetchSavingsHistory, { retry: false })
  const refresh = () => {
    qc.invalidateQueries('savings-summary'); qc.invalidateQueries('savings-plan'); qc.invalidateQueries('savings-history')
  }
  const s = q.data

  return (
    <div className="space-y-5">
      {q.isLoading && <div className="text-sm text-slate-500">Loading your accounts and live prices…</div>}
      {q.isError && <div className="card text-sm text-red-400">{errMsg(q.error)}</div>}
      {s && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="card-sm"><div className="text-[10px] uppercase tracking-wider text-slate-500">Everything tracked</div>
              <div className="text-lg font-bold text-slate-100 num mt-1">{usd0(s.total)}</div></div>
            <div className="card-sm"><div className="text-[10px] uppercase tracking-wider text-slate-500">For retirement</div>
              <div className="text-lg font-bold text-slate-100 num mt-1">{usd0(s.retirement_total)}</div></div>
            <div className="card-sm"><div className="text-[10px] uppercase tracking-wider text-slate-500">Stocks (look-through)</div>
              <div className="text-lg font-bold text-slate-100 num mt-1">{pct(s.equity_share)}</div></div>
            <div className="card-sm"><div className="text-[10px] uppercase tracking-wider text-slate-500">Illiquid</div>
              <div className="text-lg font-bold text-slate-100 num mt-1">{pct(s.illiquid_share)}</div></div>
          </div>

          {s.warnings.length > 0 && (
            <div className="card space-y-1">
              {s.warnings.map((w, i) => <div key={i} className={`text-xs ${SEV[w.severity] || 'text-slate-400'}`}>{w.detail}</div>)}
            </div>
          )}

          <div className="card grid md:grid-cols-3 gap-5">
            <Bars title="By provider" data={s.by_provider} total={s.total} />
            <Bars title="By tax treatment" data={s.by_tax} total={s.total} labels={TAX_LABEL} />
            <Bars title="By asset class" data={s.by_class} total={s.total} labels={CLASS_LABEL} />
          </div>

          {s.lookthrough?.available && s.lookthrough.top_stocks?.length > 0 && (
            <div className="card text-xs text-slate-400">
              <span className="text-slate-300 font-semibold">Biggest single stocks across all funds</span> (from top-10 holdings of {pct(s.lookthrough.coverage)} of your money):{' '}
              {s.lookthrough.top_stocks.slice(0, 5).map(t => `${t.symbol} ${t.lookthrough_pct}%`).join(' · ')}
            </div>
          )}

          {s.deferrals?.length > 0 && (
            <div className="card space-y-2">
              <div className="text-slate-100 font-semibold">401(k) deferral limit</div>
              {s.deferrals.map(d => (
                <div key={d.provider} className="text-sm">
                  <div className="flex justify-between text-slate-300">
                    <span>{d.provider} · your contributions</span>
                    <span className="num">{usd0(d.employee_planned)} of {usd0(d.limit)}</span>
                  </div>
                  <div className="h-1.5 rounded bg-white/[0.06] overflow-hidden mt-1">
                    <div className={`h-full ${d.over_by > 0 ? 'bg-red-500' : 'bg-emerald-500'}`}
                         style={{ width: `${Math.min(100, (d.employee_planned / d.limit) * 100)}%` }} />
                  </div>
                  <div className="text-xs text-slate-500 mt-1">
                    {d.over_by > 0 ? `Over the limit by ${usd0(d.over_by)}.` : `${usd0(d.room_left)} of room left.`}
                    {d.catch_up_label && ` Includes the ${usd0(d.catch_up)} ${d.catch_up_label}.`}
                    {d.employer_planned > 0 && ` Employer match of ${usd0(d.employer_planned)} does not count toward this limit.`}
                    {d.estimated && ' Limit estimated from the latest known year.'}
                  </div>
                </div>
              ))}
              <div className="text-[11px] text-slate-600">Limits are from memory: verify at irs.gov or with your plan.</div>
            </div>
          )}

          <PlanCard />
          <ChangesCard />

          <div className="card space-y-3">
            <div className="text-slate-100 font-semibold">Accounts</div>
            {s.accounts.length === 0 && <div className="text-sm text-slate-500">No accounts yet. Add your 401(k), IRAs and anything else below.</div>}
            {s.accounts.map(a => <AccountCard key={String(a.id)} a={a} onChanged={refresh} />)}
            <div className="border-t border-white/[0.05] pt-3 space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">Add an account</div>
              <AddAccount onAdded={refresh} />
            </div>
            <div className="text-[11px] text-slate-600">
              Nothing here connects to your providers or stores their logins: you enter holdings, and tickers are priced live.
              Core funds, real estate and notes use a value and an as-of date. Update them when you get a statement.
            </div>
          </div>

          <div className="card space-y-2">
            <div className="text-slate-100 font-semibold">History</div>
            <Sparkline points={hist.data} />
          </div>
        </>
      )}
    </div>
  )
}
