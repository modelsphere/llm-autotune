import { defineStore } from 'pinia'
import { api } from '../api/client'

interface User {
  id: number
  username: string
  role: string
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('autotune_token') as string | null,
    user: null as User | null,
  }),
  getters: {
    isAuthenticated: (state) => !!state.token,
  },
  actions: {
    async login(username: string, password: string) {
      const { data } = await api.post('/auth/login', { username, password })
      this.setToken(data.access_token)
      await this.fetchMe()
    },
    async register(username: string, password: string) {
      const { data } = await api.post('/auth/register', { username, password })
      this.setToken(data.access_token)
      await this.fetchMe()
    },
    async fetchMe() {
      const { data } = await api.get('/auth/me')
      this.user = data
    },
    setToken(token: string) {
      this.token = token
      localStorage.setItem('autotune_token', token)
    },
    logout() {
      this.token = null
      this.user = null
      localStorage.removeItem('autotune_token')
    },
  },
})
