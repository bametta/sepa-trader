import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from 'react-query'
import {
  fetchAdminUsers, fetchAppHealth,
  updateAdminUser, deleteAdminUser, resetAdminUserPassword,
} from '../api/client'

export default function AdminPanel() {
  const qc = useQueryClient()
  const { data: users  = [], isLoading: usersLoading  } = useQuery('admin-users',  fetchAdminUsers,  { refetchInterval: 30000 })
  const { data: health = {}, isLoading: healthLoading } = useQuery('admin-health', fetchAppHealth,   { refetchInterval: 15000 })

  const toggleActive = useMutation(
    ({ id, is_active }) => updateAdminUser(id, { is_active }),
    { onSuccess: () => qc.invalidateQueries('admin-users') },
  )
  const toggleRole = useMutation(
    ({ id, role }) => updateAdminUser(id, { role }),
    { onSuccess: () => qc.invalidateQueries('admin-users') },
  )
  const deleteUser = useMutation(
    (id) => deleteAdminUser(id),
    { onSuccess: () => qc.invalidateQueries('admin-users') },
  )
  const resetPassword = useMutation(resetAdminUserPassword)

  const [resetResult, setResetResult] = useState(null)

  async function handleReset(id) {
    const r = await resetPassword.mutateAsync(id)
    setResetResult(r.temp_password)
  }

  return (
    <div className="space-y-8">

      {/* App Health */}
      <div className="bg-card border border-border rounded-xl p-6 space-y-4">
        <h2 className="text-slate-200 font-semibold text-sm">App Health</h2>
        {healthLoading ? (
          <div className="h-16 bg-surface rounded animate-pulse" />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Stat label="Database"      value={health.db}               ok={health.db === 'ok'} />
            <Stat label="Scheduler"     value={health.scheduler_running ? 'running' : 'stopped'} ok={health.scheduler_running} />
            <Stat label="Users"         value={health.user_count}        />
            <Stat label="Trades (7d)"   value={health.trades_last_7d}    />
            <Stat label="Screener"      value={health.screener_status}   ok={health.screener_status !== 'error'} />
            <Stat label="Last Screener" value={health.last_screener_run || '—'} />
          </div>
        )}
        {health.jobs?.length > 0 && (
          <div className="mt-2 space-y-1">
            <p className="text-xs text-slate-500 font-medium">Scheduled jobs</p>
            {health.jobs.map(j => (
              <div key={j.id} className="flex justify-between text-xs text-slate-400">
                <span className="font-mono">{j.id}</span>
                <span>{j.next_run ? `next: ${new Date(j.next_run).toLocaleTimeString()}` : '—'}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Users */}
      <div className="bg-card border border-border rounded-xl p-6 space-y-4">
        <h2 className="text-slate-200 font-semibold text-sm">Users</h2>

        {resetResult && (
          <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg px-4 py-3 text-sm text-yellow-300 flex justify-between items-center">
            <span>Temporary password: <span className="font-mono font-bold">{resetResult}</span></span>
            <button onClick={() => setResetResult(null)} className="text-yellow-500 hover:text-yellow-300 text-xs">Dismiss</button>
          </div>
        )}

        {usersLoading ? (
          <div className="h-24 bg-surface rounded animate-pulse" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left border-b border-border">
                  <th className="pb-2 text-xs text-slate-500 font-medium pr-4">User</th>
                  <th className="pb-2 text-xs text-slate-500 font-medium pr-4">Role</th>
                  <th className="pb-2 text-xs text-slate-500 font-medium pr-4">2FA</th>
                  <th className="pb-2 text-xs text-slate-500 font-medium pr-4">Status</th>
                  <th className="pb-2 text-xs text-slate-500 font-medium pr-4">Last login</th>
                  <th className="pb-2 text-xs text-slate-500 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {users.map(u => (
                  <tr key={u.id} className="text-slate-300">
                    <td className="py-2.5 pr-4">
                      <div className="font-medium">{u.username}</div>
                      <div className="text-xs text-slate-500">{u.email}</div>
                    </td>
                    <td className="py-2.5 pr-4">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                        u.role === 'admin' ? 'bg-accent/20 text-accent' : 'bg-slate-700 text-slate-300'
                      }`}>
                        {u.role}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4">
                      <span className={`text-xs ${u.totp_enabled ? 'text-emerald-400' : 'text-slate-600'}`}>
                        {u.totp_enabled ? 'enabled' : 'off'}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4">
                      <span className={`text-xs ${u.is_active ? 'text-emerald-400' : 'text-red-400'}`}>
                        {u.is_active ? 'active' : 'inactive'}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-xs text-slate-500">
                      {u.last_login ? new Date(u.last_login).toLocaleDateString() : 'never'}
                    </td>
                    <td className="py-2.5">
                      <div className="flex gap-2 flex-wrap">
                        <ActionBtn
                          onClick={() => toggleActive.mutate({ id: u.id, is_active: !u.is_active })}
                          label={u.is_active ? 'Deactivate' : 'Activate'}
                          variant={u.is_active ? 'danger' : 'success'}
                        />
                        <ActionBtn
                          onClick={() => toggleRole.mutate({ id: u.id, role: u.role === 'admin' ? 'user' : 'admin' })}
                          label={u.role === 'admin' ? 'Make user' : 'Make admin'}
                        />
                        <ActionBtn
                          onClick={() => handleReset(u.id)}
                          label="Reset pw"
                        />
                        <ActionBtn
                          onClick={() => { if (window.confirm(`Delete ${u.username}?`)) deleteUser.mutate(u.id) }}
                          label="Delete"
                          variant="danger"
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, ok }) {
  return (
    <div className="bg-surface border border-border rounded-lg p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`text-sm font-medium mt-0.5 ${
        ok === true ? 'text-emerald-400' : ok === false ? 'text-red-400' : 'text-slate-200'
      }`}>
        {String(value ?? '—')}
      </div>
    </div>
  )
}

function ActionBtn({ onClick, label, variant }) {
  const base = 'text-xs px-2 py-1 rounded font-medium transition-colors'
  const cls  = variant === 'danger'  ? `${base} bg-red-500/10 text-red-400 hover:bg-red-500/20`
             : variant === 'success' ? `${base} bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20`
             :                         `${base} bg-slate-700 text-slate-300 hover:bg-slate-600`
  return <button onClick={onClick} className={cls}>{label}</button>
}
