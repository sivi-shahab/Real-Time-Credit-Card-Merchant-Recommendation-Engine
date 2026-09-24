import { VueQueryPlugin } from '@tanstack/vue-query'
import Aura from '@primevue/themes/aura'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import 'primeicons/primeicons.css'
import './style.css'

createApp(App)
  .use(createPinia())
  .use(router)
  .use(PrimeVue, { theme: { preset: Aura, options: { darkModeSelector: '.dark' } } })
  .use(ToastService)
  .use(ConfirmationService)
  .use(VueQueryPlugin, {
    queryClientConfig: {
      defaultOptions: { queries: { retry: 1, staleTime: 5_000, refetchOnWindowFocus: false } },
    },
  })
  .mount('#app')
