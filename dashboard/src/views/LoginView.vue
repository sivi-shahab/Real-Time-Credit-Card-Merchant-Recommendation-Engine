<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useSession } from '@/stores/session'

const session = useSession()
const route = useRoute()
const router = useRouter()
const token = ref('')

async function submit() {
  if (!token.value || session.loading) return   // form blocks double submit
  try {
    await session.login(token.value.trim())
    router.push(String(route.query.next ?? '/overview'))
  } catch { /* error rendered from the store */ }
}
</script>

<template>
  <form class="panel" style="width:380px" @submit.prevent="submit">
    <h3>Masuk konsol</h3>
    <p style="font-size:13px;color:#65708a">
      Token sesi demo. Produksi memakai BFF dengan cookie <code>HttpOnly</code> (SDD 13.4).
    </p>
    <label class="field" style="margin:12px 0">
      Token
      <InputText v-model="token" autofocus autocomplete="off" />
    </label>
    <Message v-if="session.error" severity="error" :closable="false">{{ session.error }}</Message>
    <Button type="submit" label="Masuk" :loading="session.loading" :disabled="!token" />
  </form>
</template>
