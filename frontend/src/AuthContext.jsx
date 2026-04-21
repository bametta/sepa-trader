import { createContext, useContext, useState, useEffect } from 'react'
import { fetchMe, login as apiLogin, loginWith2fa, register as apiRegister, logout as apiLogout } from './api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser]       = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchMe()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  async function login(email, password) {
    const data = await apiLogin(email, password)
    if (data.requires_2fa) return { requires_2fa: true, temp_token: data.temp_token }
    const me = await fetchMe()
    setUser(me)
    return { requires_2fa: false }
  }

  async function verify2fa(temp_token, code) {
    await loginWith2fa(temp_token, code)
    const me = await fetchMe()
    setUser(me)
  }

  async function register(email, username, password) {
    await apiRegister(email, username, password)
    const me = await fetchMe()
    setUser(me)
  }

  async function logout() {
    await apiLogout()
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, verify2fa, register, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
