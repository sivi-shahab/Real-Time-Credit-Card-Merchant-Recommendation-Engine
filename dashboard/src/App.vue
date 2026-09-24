<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'
import { navItems } from './router'
import { useSession } from '@/stores/session'

const session = useSession()
const router = useRouter()
onMounted(() => session.restore())

function logout() {
  session.logout()
  router.push('/login')
}
</script>

<template>
  <Toast />
  <ConfirmDialog />
  <div v-if="!session.authenticated" class="plain-shell"><RouterView /></div>
  <div v-else class="app">
    <nav class="sidebar" aria-label="Navigasi utama">
      <h1>Recommendation Engine</h1>
      <RouterLink
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        v-show="session.can(String(item.meta?.permission))"
      >
        <i :class="['pi', item.meta?.icon]" aria-hidden="true" />
        <span>{{ item.meta?.title }}</span>
      </RouterLink>
      <div class="who">
        <div>{{ session.me?.subject }}</div>
        <div>{{ session.me?.role }}</div>
        <a href="#" @click.prevent="logout">Keluar</a>
      </div>
    </nav>
    <main><RouterView /></main>
  </div>
</template>

<style scoped>
.sidebar { display: flex; flex-direction: column; }
.plain-shell { display: grid; place-items: center; min-height: 100vh; }
</style>
