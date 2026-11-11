<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-brand">
        <div class="logo-box">DA</div>
        <div>
          <div class="logo-name">DmfDataAgent</div>
          <div class="logo-sub">指标问答 Agent · 登录</div>
        </div>
      </div>

      <el-form :model="form" @submit.prevent="onSubmit" class="login-form">
        <el-form-item>
          <el-input
            v-model="form.username"
            size="large"
            placeholder="用户名"
            :prefix-icon="User"
            autocomplete="username"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            show-password
            placeholder="密码"
            :prefix-icon="Lock"
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-alert
          v-if="store.auth.error"
          :title="store.auth.error"
          type="error"
          :closable="false"
          show-icon
          class="login-err"
        />
        <el-button
          type="primary"
          size="large"
          class="login-btn"
          :loading="store.auth.loading"
          @click="onSubmit"
        >
          登 录
        </el-button>
      </el-form>

      <div class="login-hint">
        首次部署默认管理员：<b>admin / admin123</b>（登录后请尽快修改密码）
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { login, store } from '../store'

const form = reactive({ username: '', password: '' })

async function onSubmit() {
  if (!form.username || !form.password) {
    store.auth.error = '请输入用户名和密码'
    return
  }
  const ok = await login(form.username.trim(), form.password)
  if (ok) {
    ElMessage({ message: '登录成功', type: 'success' })
  }
}
</script>

<style scoped>
.login-wrap {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #eef3ff 0%, #f7f9fc 55%, #fdf3e8 100%);
}
.login-card {
  width: 380px;
  padding: 34px 34px 26px;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 12px 40px rgba(20, 40, 90, 0.12);
}
.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 26px;
}
.logo-box {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 16px;
  color: #fff;
  background: linear-gradient(90deg, #ffa244, #ff4800);
}
.logo-name { font-size: 17px; font-weight: 600; color: #1f2329; }
.logo-sub { font-size: 12px; color: #8a9099; margin-top: 2px; }
.login-form { margin-top: 4px; }
.login-err { margin-bottom: 12px; }
.login-btn { width: 100%; letter-spacing: 4px; }
.login-hint {
  margin-top: 18px;
  font-size: 12px;
  color: #98a0aa;
  text-align: center;
  line-height: 1.6;
}
.login-hint b { color: #ff4800; font-weight: 600; }
</style>
