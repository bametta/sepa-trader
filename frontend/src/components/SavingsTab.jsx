/**
 * Savings tab: every account across providers in one place (manual-first, no credentials stored).
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import {
  fetchSavingsSummary, fetchSavingsPlan, fetchSavingsHistory, fetchSavingsChanges, createSavingsAccount, patchSavingsAccount,
  importSavingsPositions, fetchTargetModel, runTradePlan, updateSetting, updateManualValues,
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

function ImportPanel({ accountId, onDone, onCancel }) {
  const [text, setText] = useState('')
  const [accountKey, setAccountKey] = useState(null)
  const preview = useMutation(importSavingsPositions)
  const apply = useMutation(importSavingsPositions, { onSuccess: onDone })
  const p = preview.data
  const ready = p && !p.needs_choice
  const readFile = async e => {
    const f = e.target.files?.[0]
    if (!f) return
    setText(await f.text())
    setAccountKey(null)
    preview.reset()
  }
  return (
    <div className="space-y-2 rounded-lg border border-white/[0.08] p-3">
      <div className="text-[11px] text-slate-500">
        Download your positions from the broker (Schwab: Positions → Export. Fidelity: Positions → Download, the arrow icon),
        then choose that file here or paste it below. If the file holds several accounts you'll pick one. You'll see what
        would change before anything is saved.
      </div>
      <input type="file" accept=".csv,text/csv,text/plain" onChange={readFile} className="text-xs text-slate-400" />
      <textarea className="input w-full font-mono text-xs" rows={5} placeholder="…or paste the file contents here"
                value={text} onChange={e => { setText(e.target.value); setAccountKey(null); preview.reset() }} spellCheck={false} />
      <div className="flex gap-2">
        <button className="btn-ghost" disabled={!text.trim() || preview.isLoading}
                onClick={() => preview.mutate({ id: accountId, text, apply: false, account_key: accountKey })}>
          {preview.isLoading ? 'Reading…' : 'Preview changes'}
        </button>
        {ready && (
          <button className="btn-primary" disabled={apply.isLoading}
                  onClick={() => apply.mutate({ id: accountId, text, apply: true, account_key: accountKey })}>
            {apply.isLoading ? 'Saving…' : `Save ${p.holdings} holdings`}
          </button>
        )}
        <button className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
      {(preview.isError || apply.isError) && <div className="text-sm text-red-400">{errMsg(preview.error || apply.error)}</div>}
      {p?.needs_choice && (
        <div className="space-y-1 text-xs">
          <div className="text-slate-300">This file contains {p.choices.length} accounts. Which one is this?</div>
          {p.choices.map(c => (
            <button key={c.key} className="block w-full text-left rounded border border-white/[0.08] px-2 py-1 hover:bg-white/[0.04]"
                    onClick={() => { setAccountKey(c.key); preview.mutate({ id: accountId, text, apply: false, account_key: c.key }) }}>
              <span className="text-slate-200">{c.label}</span>
              <span className="text-slate-500"> · {c.holdings} holdings · {usd0(c.total)}</span>
            </button>
          ))}
        </div>
      )}
      {ready && (
        <div className="space-y-1 text-xs">
          <div className="text-slate-300">
            {p.source === 'broker' ? 'Broker export' : 'CSV'}{p.title ? ` · ${p.title}` : ''}: {p.holdings} holdings, {usd0(p.parsed_total)} at live prices
            {p.file_total != null && ` (the file's own total is ${usd0(p.file_total)}, from the time you exported it)`}.
            Currently stored: {usd0(p.stored_total)}.
          </div>
          {p.warnings.map((w, i) => <div key={i} className="text-amber-400">{w}</div>)}
          {p.diff.events.length === 0 && <div className="text-slate-500">No holdings changes: the same positions as stored.</div>}
          {p.diff.events.map((e, i) => <div key={i} className="text-slate-400">• {(EVENT_TEXT[e.type] || (() => e.type))(e)}</div>)}
        </div>
      )}
    </div>
  )
}

function ValuesPanel({ accountId, rows, onDone, onCancel }) {
  const today = new Date().toISOString().slice(0, 10)
  const original = Object.fromEntries(rows.map(h => [h.id, { value: String(h.manual_value ?? h.value ?? ''), as_of: h.as_of || today }]))
  const [edits, setEdits] = useState(original)
  const save = useMutation(updateManualValues, { onSuccess: onDone })
  const set = (id, patch) => setEdits(e => ({ ...e, [id]: { ...e[id], ...patch } }))
  const changed = rows.filter(h => edits[h.id].value !== original[h.id].value || edits[h.id].as_of !== original[h.id].as_of)
  const invalid = changed.some(h => !(parseFloat(edits[h.id].value) >= 0))

  return (
    <div className="space-y-2 rounded-lg border border-white/[0.08] p-3">
      <div className="text-[11px] text-slate-500">
        Update the value of anything you value by hand (real estate, notes, funds without a ticker). Use your latest statement,
        an appraisal or your own estimate. Custodians like Equity Trust don't verify these values for you. Changing a value sets
        its date to today; "still correct" keeps the value and refreshes the date. Everything saves together.
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wider text-slate-500">
            <th className="py-1">Holding</th><th className="text-right">Value $</th><th className="text-right">As of</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map(h => {
            const e = edits[h.id]
            const dirty = e.value !== original[h.id].value || e.as_of !== original[h.id].as_of
            return (
              <tr key={h.id} className="border-t border-white/[0.05]">
                <td className="py-1 pr-2 text-slate-300">
                  {h.name}{h.stale && <span className="ml-2 text-amber-400">out of date</span>}
                </td>
                <td className="text-right">
                  <input className="input w-28 text-right py-0" type="number" min="0" step="0.01" value={e.value}
                         onChange={ev => set(h.id, { value: ev.target.value, as_of: today })} />
                </td>
                <td className="text-right">
                  <input className="input w-32 py-0" type="date" max={today} value={e.as_of}
                         onChange={ev => set(h.id, { as_of: ev.target.value })} />
                </td>
                <td className="text-right pl-2">
                  <button className="underline text-slate-500 hover:text-slate-200 disabled:no-underline disabled:opacity-40"
                          disabled={e.as_of === today && !dirty ? true : false} onClick={() => set(h.id, { as_of: today })}>
                    still correct
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="flex gap-2">
        <button className="btn-primary" disabled={changed.length === 0 || invalid || save.isLoading}
                onClick={() => save.mutate({ id: accountId, updates: changed.map(h => ({
                  holding_id: h.id, value: parseFloat(edits[h.id].value), as_of: edits[h.id].as_of })) })}>
          {save.isLoading ? 'Saving…' : `Save ${changed.length} change${changed.length === 1 ? '' : 's'}`}
        </button>
        <button className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
      {save.isError && <div className="text-sm text-red-400">{errMsg(save.error)}</div>}
    </div>
  )
}

function AccountCard({ a, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [csv, setCsv] = useState('')
  const [contrib, setContrib] = useState(a.annual_contribution)
  const [importing, setImporting] = useState(false)
  const [valuing, setValuing] = useState(false)
  const handValued = a.holdings.filter(h => h.id != null && h.quantity == null && h.source !== 'live')
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
            <div className="text-[11px] text-slate-500 space-y-0.5">
              {a.max_deferral
                ? <div className="text-right">saves <span className="num text-slate-300">{usd0(a.annual_contribution)}</span>/yr · the IRS limit</div>
                : (
                  <div className="flex items-center gap-1 justify-end">
                    saves
                    <input className="input w-20 text-right py-0" type="number" min="0" value={contrib}
                           onChange={e => setContrib(e.target.value)}
                           onBlur={() => parseFloat(contrib) !== a.annual_contribution && patch.mutate({ id: a.id, annual_contribution: parseFloat(contrib) || 0 })} />
                    /yr
                  </div>
                )}
              {['401k', 'roth_401k'].includes(a.account_type) && a.contribution_source === 'employee' && (
                <label className="flex items-center gap-1 justify-end cursor-pointer">
                  <input type="checkbox" checked={!!a.max_deferral} disabled={patch.isLoading}
                         onChange={e => patch.mutate({ id: a.id, max_deferral: e.target.checked })} />
                  max the IRS limit every year
                </label>
              )}
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
          <button className="underline text-slate-400 hover:text-slate-200" onClick={() => setImporting(v => !v)}>
            Import positions file
          </button>
          {handValued.length > 0 && (
            <button className="underline text-slate-400 hover:text-slate-200" onClick={() => setValuing(v => !v)}>
              Update values
            </button>
          )}
          <button className="underline text-slate-600 hover:text-red-400"
                  onClick={() => window.confirm(`Delete ${a.provider} ${a.name} and all its holdings?`) && del.mutate(a.id)}>
            Delete account
          </button>
        </div>
      )}
      {importing && !a.read_only && (
        <ImportPanel accountId={a.id} onDone={() => { setImporting(false); onChanged() }} onCancel={() => setImporting(false)} />
      )}
      {valuing && !a.read_only && handValued.length > 0 && (
        <ValuesPanel accountId={a.id} rows={handValued} onDone={() => { setValuing(false); onChanged() }}
                     onCancel={() => setValuing(false)} />
      )}
      {(load.isError || patch.isError || del.isError) && (
        <div className="text-sm text-red-400">{errMsg(load.error || patch.error || del.error)}</div>
      )}
    </div>
  )
}

function AddAccount({ onAdded }) {
  const blank = { provider: '', name: '', account_type: '401k', annual_contribution: '', contribution_source: 'employee', max_deferral: false, retirement: true }
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
        <input className="input w-full" type="number" min="0" placeholder={f.max_deferral ? 'follows the IRS limit' : 'this account only'}
               value={f.max_deferral ? '' : f.annual_contribution} disabled={f.max_deferral}
               onChange={e => setF({ ...f, annual_contribution: e.target.value })} />
        {['401k', 'roth_401k'].includes(f.account_type) && f.contribution_source === 'employee' && (
          <span className="flex items-center gap-1 normal-case tracking-normal text-[11px] mt-1 cursor-pointer">
            <input type="checkbox" checked={f.max_deferral} onChange={e => setF({ ...f, max_deferral: e.target.checked })} />
            max the IRS limit every year
          </span>
        )}
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

function planText(plan) {
  const lines = [`Trade worksheet (${plan.mode === 'full' ? 'full rebalance' : 'cash only'}), target ${plan.target_text}`]
  plan.accounts.forEach(a => {
    lines.push('', `${a.name} (${a.tax === 'roth' ? 'Roth' : 'pre-tax'}), cash ${usd0(a.cash_before)}:`)
    if (!a.trades.length) lines.push('  no trades')
    a.trades.forEach(t => lines.push(`  ${t.side.toUpperCase()} ${t.shares} ${t.symbol} @ ~$${t.price.toFixed(2)} (~${usd0(t.value)})`))
  })
  return lines.join('\n')
}

function TradeHelper({ accounts }) {
  const eligible = accounts.filter(a => !a.read_only && ['roth', 'pre_tax'].includes(a.tax))
  const info = useQuery('savings-target-model', fetchTargetModel, { retry: false })
  const [picked, setPicked] = useState(null)
  const [mode, setMode] = useState('cash_only')
  const [target, setTarget] = useState(null)
  const plan = useMutation(runTradePlan)
  const save = useMutation(() => updateSetting('savings_target_model', target ?? info.data?.effective ?? ''))
  if (eligible.length === 0) return null
  const ids = picked ?? eligible.map(a => a.id)
  const text = target ?? info.data?.effective ?? ''
  const p = plan.data

  return (
    <div className="card space-y-3">
      <div className="text-slate-100 font-semibold">Trade helper</div>
      <div className="text-xs text-slate-500">
        A worksheet of whole-share trades for the accounts you pick, so together they move toward your target. It uses only each
        account's own cash and holdings and places nothing: you place the orders yourself.
      </div>
      <div className="flex flex-wrap gap-4 text-sm">
        {eligible.map(a => (
          <label key={a.id} className="flex items-center gap-2 text-slate-300">
            <input type="checkbox" checked={ids.includes(a.id)}
                   onChange={e => setPicked(e.target.checked ? [...ids, a.id] : ids.filter(i => i !== a.id))} />
            {a.provider} · {a.name} <span className="text-slate-500">({a.tax === 'roth' ? 'Roth' : 'pre-tax'}, {usd0(a.value)})</span>
          </label>
        ))}
      </div>
      <div className="grid md:grid-cols-3 gap-3 items-end">
        <label className="text-[10px] uppercase tracking-wider text-slate-500 md:col-span-2">
          Target allocation (symbol:percent)
          <input className="input w-full font-mono text-xs" value={text} placeholder="VTI:50, VXUS:25, BND:25"
                 onChange={e => setTarget(e.target.value)} />
        </label>
        <label className="text-[10px] uppercase tracking-wider text-slate-500">Mode
          <select className="input w-full" value={mode} onChange={e => setMode(e.target.value)}>
            <option value="cash_only">Use new cash only (no sells)</option>
            <option value="full">Full rebalance (may sell)</option>
          </select>
        </label>
      </div>
      <div className="text-[11px] text-slate-500">
        {info.data?.saved ? 'Using your saved target.' : info.data?.default
          ? 'Starting from a plain three-fund mix based on your age and risk. Edit it freely.' : ''}
        {' '}Bonds and cash are placed in the pre-tax account and growth funds in the Roth where room allows.
      </div>
      <div className="flex gap-2">
        <button className="btn-primary" disabled={plan.isLoading || ids.length === 0}
                onClick={() => plan.mutate({ account_ids: ids, mode, target: text })}>
          {plan.isLoading ? 'Calculating…' : 'Build worksheet'}
        </button>
        <button className="btn-ghost" disabled={save.isLoading || !text} onClick={() => save.mutate()}>
          {save.isLoading ? 'Saving…' : save.isSuccess ? 'Saved as default ✓' : 'Save target as default'}
        </button>
      </div>
      {plan.isError && <div className="text-sm text-red-400">{errMsg(plan.error)}</div>}
      {p && (
        <div className="space-y-3">
          {p.accounts.map(a => (
            <div key={a.id} className="rounded-lg border border-white/[0.08] p-3 space-y-1">
              <div className="flex justify-between text-sm">
                <span className="text-slate-200 font-semibold">{a.name} <span className="text-slate-500 font-normal">({a.tax === 'roth' ? 'Roth' : 'pre-tax'})</span></span>
                <span className="text-slate-500 num">cash {usd0(a.cash_before)} → {usd0(a.cash_after)}</span>
              </div>
              {a.trades.length === 0 && <div className="text-xs text-slate-500">No trades.</div>}
              {a.trades.map((t, i) => (
                <div key={i} className="flex justify-between text-sm num">
                  <span className={t.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>
                    {t.side.toUpperCase()} {t.shares} {t.symbol}
                  </span>
                  <span className="text-slate-400">@ ~${t.price.toFixed(2)} · ~{usd0(t.value)}</span>
                </div>
              ))}
            </div>
          ))}
          <div className="text-xs text-slate-400">
            Biggest gap from target: {pct(p.max_drift_before, 1)} before, {pct(p.max_drift_after, 1)} after.
            {' '}After: {Object.entries(p.after).slice(0, 6).map(([s, w]) => `${s} ${pct(w, 1)}`).join(' · ')}
            {' '}(target {Object.entries(p.target).map(([s, w]) => `${s} ${pct(w, 0)}`).join(' · ')}).
          </div>
          {p.notes.map((n, i) => <div key={i} className="text-xs text-amber-400">{n}</div>)}
          {p.excluded.length > 0 && (
            <div className="text-xs text-slate-500">
              Not traded here (no ticker/price): {p.excluded.map(e => `${e.name} ${usd0(e.value)}`).join(', ')}.
            </div>
          )}
          <div className="flex items-center gap-3">
            <button className="btn-ghost" onClick={() => navigator.clipboard?.writeText(planText(p))}>Copy worksheet</button>
            <span className="text-[11px] text-slate-600">
              {p.prices_as_of}. Check your plan's trading rules and fees first. Planning aid, not financial advice.
            </span>
          </div>
        </div>
      )}
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

          <TradeHelper accounts={s.accounts} />
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
