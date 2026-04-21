import { useQuery } from 'react-query'
import { fetchOpenOrders, fetchAlpacaHistory } from '../api/client'

const SIDE_CLASS   = { buy: 'text-emerald-400', sell: 'text-red-400' }
const STATUS_CLASS = {
  filled:           'bg-emerald-500/12 text-emerald-400 border-emerald-500/25',
  partially_filled: 'bg-amber-500/12   text-amber-400   border-amber-500/25',
  canceled:         'bg-slate-500/10   text-slate-500   border-slate-500/20',
  expired:          'bg-slate-500/10   text-slate-500   border-slate-500/20',
  pending_new:      'bg-blue-500/12    text-blue-400    border-blue-500/25',
  new:              'bg-blue-500/12    text-blue-400    border-blue-500/25',
}

function StatusBadge({ status }) {
  const clean = status?.replace('OrderStatus.', '').toLowerCase() || ''
  const cls   = STATUS_CLASS[clean] || 'bg-slate-500/10 text-slate-500 border-slate-500/20'
  return (
    <span className={`inline-block text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-lg border ${cls}`}>
      {clean || '—'}
    </span>
  )
}

function TableShell({ headers, children, empty }) {
  return (
    <div className="overflow-x-auto -mx-1">
      <table className="w-full text-xs">
        <thead>
          <tr>
            {headers.map(h => (
              <th key={h} className="label text-left pb-3 pr-4 first:pl-1">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {children}
        </tbody>
      </table>
      {empty && (
        <div className="text-center py-8">
          <p className="text-slate-600 text-sm">{empty}</p>
        </div>
      )}
    </div>
  )
}

function SectionCard({ title, loading, children }) {
  return (
    <div className="card p-5">
      <h3 className="label mb-4">{title}</h3>
      {loading
        ? <div className="space-y-2">{[...Array(3)].map((_, i) => <div key={i} className="h-8 bg-white/5 rounded-lg animate-pulse" />)}</div>
        : children
      }
    </div>
  )
}

export function OpenOrdersTable() {
  const { data = [], isLoading } = useQuery('openOrders', () => fetchOpenOrders(), { refetchInterval: 15000 })

  const headers = ['Symbol', 'Side', 'Qty', 'Type', 'Status', 'Submitted']

  return (
    <SectionCard title={`Open Orders${data.length ? ` (${data.length})` : ''}`} loading={isLoading}>
      {data.length === 0 ? (
        <div className="text-center py-8">
          <p className="text-slate-600 text-sm">No open orders</p>
        </div>
      ) : (
        <TableShell headers={headers}>
          {data.map((o, i) => {
            const side = o.side?.replace('OrderSide.', '').toLowerCase()
            return (
              <tr key={o.id} className={`border-t ${i === 0 ? 'border-white/5' : 'border-white/[0.03]'} hover:bg-white/[0.02] transition-colors`}>
                <td className="py-2.5 pr-4 pl-1 font-bold text-slate-100 num">{o.symbol}</td>
                <td className={`py-2.5 pr-4 font-semibold ${SIDE_CLASS[side] || 'text-slate-400'}`}>
                  {side?.toUpperCase() || '—'}
                </td>
                <td className="py-2.5 pr-4 text-slate-300 num">{o.qty}</td>
                <td className="py-2.5 pr-4 text-slate-500">{o.type?.replace('OrderType.', '')}</td>
                <td className="py-2.5 pr-4"><StatusBadge status={o.status} /></td>
                <td className="py-2.5 text-slate-600 num">
                  {o.submitted_at ? new Date(o.submitted_at).toLocaleString() : '—'}
                </td>
              </tr>
            )
          })}
        </TableShell>
      )}
    </SectionCard>
  )
}

export function AlpacaHistoryTable() {
  const { data = [], isLoading } = useQuery('alpacaHistory', () => fetchAlpacaHistory(), { staleTime: 60000 })

  const headers = ['Symbol', 'Side', 'Qty', 'Filled', 'Avg Price', 'Status', 'Submitted', 'Filled At']

  return (
    <SectionCard title={`Order History${data.length ? ` (${data.length})` : ''}`} loading={isLoading}>
      {data.length === 0 ? (
        <div className="text-center py-8">
          <p className="text-slate-600 text-sm">No orders found</p>
        </div>
      ) : (
        <TableShell headers={headers}>
          {data.map((o, i) => {
            const side   = o.side?.replace('OrderSide.', '').toLowerCase()
            const status = o.status?.replace('OrderStatus.', '').toLowerCase()
            return (
              <tr key={o.id} className={`border-t ${i === 0 ? 'border-white/5' : 'border-white/[0.03]'} hover:bg-white/[0.02] transition-colors`}>
                <td className="py-2.5 pr-4 pl-1 font-bold text-slate-100 num">{o.symbol}</td>
                <td className={`py-2.5 pr-4 font-semibold ${SIDE_CLASS[side] || 'text-slate-400'}`}>
                  {side?.toUpperCase() || '—'}
                </td>
                <td className="py-2.5 pr-4 text-slate-300 num">{o.qty}</td>
                <td className="py-2.5 pr-4 text-slate-300 num">{o.filled_qty || '—'}</td>
                <td className="py-2.5 pr-4 text-slate-300 num">{o.filled_avg ? `$${Number(o.filled_avg).toFixed(2)}` : '—'}</td>
                <td className="py-2.5 pr-4"><StatusBadge status={o.status} /></td>
                <td className="py-2.5 pr-4 text-slate-600 num">{o.submitted_at ? new Date(o.submitted_at).toLocaleString() : '—'}</td>
                <td className="py-2.5 text-slate-600 num">{o.filled_at ? new Date(o.filled_at).toLocaleString() : '—'}</td>
              </tr>
            )
          })}
        </TableShell>
      )}
    </SectionCard>
  )
}
