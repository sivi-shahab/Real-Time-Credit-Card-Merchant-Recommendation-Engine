import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useSession } from '@/stores/session'

/** `permission` mirrors the backend guard. The UI hides what the API would refuse;
 *  the API refuses regardless (SEC-001) — this is convenience, not enforcement. */
const routes: RouteRecordRaw[] = [
  { path: '/login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  { path: '/', redirect: '/overview' },
  { path: '/overview', component: () => import('@/views/OverviewView.vue'),
    meta: { title: 'Ikhtisar', permission: 'metrics:read', icon: 'pi-chart-line' } },
  { path: '/datasets', component: () => import('@/views/DataStudioView.vue'),
    meta: { title: 'Data Sintetis', permission: 'dataset:read', icon: 'pi-database' } },
  { path: '/simulations', component: () => import('@/views/SimulatorView.vue'),
    meta: { title: 'Simulator', permission: 'simulation:control', icon: 'pi-play' } },
  { path: '/transactions', component: () => import('@/views/TransactionsView.vue'),
    meta: { title: 'Transaksi', permission: 'transaction:read', icon: 'pi-list' } },
  { path: '/customers', component: () => import('@/views/CustomerProfileView.vue'),
    meta: { title: 'Profil Nasabah', permission: 'customer:read', icon: 'pi-user' } },
  { path: '/explorer', component: () => import('@/views/RecommendationExplorerView.vue'),
    meta: { title: 'Recommendation Explorer', permission: 'recommendation:preview',
            icon: 'pi-search' } },
  { path: '/merchants', component: () => import('@/views/MerchantsView.vue'),
    meta: { title: 'Merchant', permission: 'merchant:read', icon: 'pi-shop' } },
  { path: '/promotions', component: () => import('@/views/PromotionsView.vue'),
    meta: { title: 'Promo', permission: 'promotion:read', icon: 'pi-tag' } },
  { path: '/audit', component: () => import('@/views/AuditView.vue'),
    meta: { title: 'Audit', permission: 'audit:read', icon: 'pi-shield' } },
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const session = useSession()
  if (to.meta.public) return true
  if (!session.authenticated) await session.restore()
  if (!session.authenticated) return { path: '/login', query: { next: to.fullPath } }
  return true
})

export const navItems = routes.filter((r) => r.meta?.title)
