<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const { t } = useI18n()

// Where to land after signing in. Someone who arrived from a link — a report
// or a run someone sent them, say — was sent here holding the page they
// actually wanted; take them there instead of to the campaigns list. Only
// an in-app path is honoured, so a crafted ?redirect= cannot bounce anyone off
// this origin.
function afterLogin(): string {
  const wanted = route.query.redirect
  const path = typeof wanted === 'string' ? wanted : ''
  return path.startsWith('/') && !path.startsWith('//') ? path : '/campaigns'
}

const username = ref('')
const password = ref('')
const registering = ref(false)
const busy = ref(false)

async function submit() {
  busy.value = true
  try {
    if (registering.value) await auth.register(username.value, password.value)
    else await auth.login(username.value, password.value)
    router.push(afterLogin())
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? t('login.failed'))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h1 class="page-title">LLM Autotune</h1>
      <p class="muted">{{ t('login.tagline') }}</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item :label="t('login.username')">
          <el-input v-model="username" autocomplete="username" />
        </el-form-item>
        <el-form-item :label="t('login.password')">
          <el-input v-model="password" type="password" autocomplete="current-password"
            @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" :loading="busy" style="width: 100%" @click="submit">
          {{ registering ? t('login.createAccount') : t('login.logIn') }}
        </el-button>
      </el-form>
      <el-button link class="switch" @click="registering = !registering">
        {{ registering ? t('login.haveAccount') : t('login.newHere') }}
      </el-button>
    </el-card>
  </div>
</template>

<style scoped>
.login-wrap {
  display: flex;
  justify-content: center;
  padding-top: 10vh;
}
.login-card {
  width: 360px;
}
.switch {
  margin-top: 12px;
}
</style>
