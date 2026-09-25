<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { get } from '@/lib/api'
import { useSession, type AuthConfig } from '@/stores/session'

const session = useSession()
const route = useRoute()
const router = useRouter()
const token = ref('')
const config = ref<AuthConfig | null>(null)
const next = String(route.query.next ?? '/overview')

onMounted(async () => {
  try { config.value = await get<AuthConfig>('/bff/config') } catch { config.value = null }
})

function sso() {
  window.location.assign(`/bff/login?next=${encodeURIComponent(next)}`)
}

async function submit() {
  if (!token.value || session.loading) return   // form blocks double submit
  try {
    await session.devLogin(token.value.trim())
    router.push(next)
  } catch { /* error rendered from the store */ }
}
</script>

<template>
  <div class="panel" style="width:380px">
    <h3>Masuk konsol</h3>
    <Message v-if="config && !config.sso && !config.devLogin" severity="warn" :closable="false">
      SSO belum dikonfigurasi.
    </Message>
    <Button v-if="config?.sso" label="Masuk dengan SSO" icon="pi pi-sign-in" @click="sso" />
    <form v-if="config?.devLogin" style="margin-top:16px" @submit.prevent="submit">
      <p style="font-size:13px;color:#65708a">
        Login token demo — hanya lingkungan lokal/uji. Sesi tetap cookie <code>HttpOnly</code>.
      </p>
      <label class="field" style="margin:12px 0">
        Token
        <InputText v-model="token" autocomplete="off" />
      </label>
      <Message v-if="session.error" severity="error" :closable="false">{{ session.error }}</Message>
      <Button type="submit" label="Masuk" severity="secondary" :loading="session.loading"
              :disabled="!token" />
    </form>
  </div>
</template>
