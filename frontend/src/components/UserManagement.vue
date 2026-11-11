<template>
  <div class="users-page">
    <div class="um-head">
      <div>
        <h2 class="um-title">用户与权限</h2>
        <div class="um-sub">租户隔离 · 角色管控（admin / analyst / viewer）</div>
      </div>
      <div class="um-head-actions">
        <el-button :icon="Refresh" @click="loadAdminMeta">刷新</el-button>
        <el-button :icon="Key" @click="selfPwdVisible = true">修改我的密码</el-button>
      </div>
    </div>

    <el-alert v-if="store.admin.error" :title="store.admin.error" type="error" :closable="false" show-icon class="um-alert" />

    <el-tabs v-model="tab" class="um-tabs">
      <!-- 用户 -->
      <el-tab-pane label="用户" name="users">
        <div class="um-toolbar">
          <el-select v-model="store.admin.tenantFilter" placeholder="全部租户" clearable style="width: 200px" @change="setUserTenantFilter">
            <el-option v-for="t in store.admin.tenants" :key="t.id" :label="`${t.name} (${t.code})`" :value="t.id" />
          </el-select>
          <el-button type="primary" :icon="Plus" @click="openCreateUser">新增用户</el-button>
        </div>
        <el-table :data="store.admin.users" v-loading="store.admin.loading" size="default" border>
          <el-table-column prop="username" label="用户名" min-width="130" />
          <el-table-column prop="display_name" label="姓名" min-width="110" />
          <el-table-column label="租户" min-width="130">
            <template #default="{ row }">{{ row.tenant_code || row.tenant_id }}</template>
          </el-table-column>
          <el-table-column label="角色" width="110">
            <template #default="{ row }">
              <el-tag :type="roleTag(row.role)" effect="light">{{ roleLabel(row.role) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain">
                {{ row.status === 'active' ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最近登录" min-width="160">
            <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="250" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openResetPwd(row)">改密</el-button>
              <el-button link type="warning" @click="toggleStatus(row)">
                {{ row.status === 'active' ? '停用' : '启用' }}
              </el-button>
              <el-button link type="danger" @click="onDeleteUser(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 租户 -->
      <el-tab-pane label="租户" name="tenants">
        <div class="um-toolbar">
          <el-button type="primary" :icon="Plus" @click="tenantDialog = true">新增租户</el-button>
        </div>
        <el-table :data="store.admin.tenants" v-loading="store.admin.loading" border>
          <el-table-column prop="code" label="编码" min-width="140" />
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain">
                {{ row.status === 'active' ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="170">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button link type="warning" @click="toggleTenant(row)">
                {{ row.status === 'active' ? '停用' : '启用' }}
              </el-button>
              <el-button link type="danger" @click="onDeleteTenant(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 角色 -->
      <el-tab-pane label="角色" name="roles">
        <div class="um-toolbar">
          <el-button type="primary" :icon="Plus" @click="openCreateRole">新增角色</el-button>
          <span class="um-hint">内置角色（admin/analyst/viewer）不可删、权限不可改；自定义角色可勾选权限点</span>
        </div>
        <el-table :data="store.admin.roles" border>
          <el-table-column label="角色" width="140">
            <template #default="{ row }">
              <el-tag :type="roleTag(row.name)" effect="light">{{ row.label || roleLabel(row.name) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="标识" width="120" />
          <el-table-column label="类型" width="90">
            <template #default="{ row }">
              <el-tag :type="row.is_builtin ? 'primary' : 'success'" effect="plain" size="small">
                {{ row.is_builtin ? '内置' : '自定义' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain" size="small">
                {{ row.status === 'active' ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
          <el-table-column label="权限点" min-width="260">
            <template #default="{ row }">
              <el-tag v-for="p in row.permissions" :key="p" size="small" effect="plain" class="perm-tag">
                {{ p === '*' ? '全部（*）' : p }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEditRole(row)">编辑</el-button>
              <el-button v-if="!row.is_builtin" link type="warning" @click="toggleRole(row)">
                {{ row.status === 'active' ? '停用' : '启用' }}
              </el-button>
              <el-button v-if="!row.is_builtin" link type="danger" @click="onDeleteRole(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新增 / 编辑角色 -->
    <el-dialog v-model="roleDialog" :title="roleEditing ? `编辑角色：${roleForm.name}` : '新增角色'" width="640px">
      <el-form :model="roleForm" label-width="80px">
        <el-form-item label="标识">
          <el-input v-model="roleForm.name" :disabled="roleEditing" placeholder="小写字母/数字/下划线，2~32 位，如 data_steward" />
        </el-form-item>
        <el-form-item label="显示名"><el-input v-model="roleForm.label" placeholder="如：数据专员" /></el-form-item>
        <el-form-item label="说明">
          <el-input v-model="roleForm.description" type="textarea" :rows="2" placeholder="角色职责说明" />
        </el-form-item>
        <el-form-item label="权限点">
          <div v-if="rolePermLocked" class="um-hint" style="margin-bottom: 6px">
            内置角色的权限固定不可修改，仅可调整展示信息
          </div>
          <div v-else-if="!permissionGroups.length" class="um-hint">
            权限点清单加载失败或为空
            <el-button link size="small" @click="reloadPermissionCatalog">重试</el-button>
          </div>
          <div v-else class="perm-groups">
            <div v-for="group in permissionGroups" :key="group.label" class="perm-group">
              <div class="perm-group-title">
                {{ group.label }}
                <el-button link size="small" @click="togglePermGroup(group)">全选/清空</el-button>
              </div>
              <el-checkbox-group v-model="roleForm.permissions" :disabled="rolePermLocked">
                <el-checkbox v-for="p in group.items" :key="p" :value="p" :label="p" />
              </el-checkbox-group>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSaveRole">
          {{ roleEditing ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 新增用户 -->
    <el-dialog v-model="userDialog" title="新增用户" width="460px">
      <el-form :model="userForm" label-width="80px">
        <el-form-item label="用户名"><el-input v-model="userForm.username" placeholder="登录名（唯一）" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="userForm.password" type="password" show-password placeholder="至少 6 位" /></el-form-item>
        <el-form-item label="姓名"><el-input v-model="userForm.display_name" placeholder="显示名称" /></el-form-item>
        <el-form-item label="租户">
          <el-select v-model="userForm.tenant_id" placeholder="选择租户" style="width: 100%">
            <el-option v-for="t in store.admin.tenants" :key="t.id" :label="`${t.name} (${t.code})`" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="userForm.role" style="width: 100%">
            <el-option v-for="r in store.admin.roles" :key="r.name" :label="r.label" :value="r.name" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onCreateUser">创建</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetDialog" :title="`重置密码：${resetTarget?.username || ''}`" width="420px">
      <el-input v-model="newPassword" type="password" show-password placeholder="新密码（至少 6 位）" />
      <template #footer>
        <el-button @click="resetDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onResetPwd">确定</el-button>
      </template>
    </el-dialog>

    <!-- 新增租户 -->
    <el-dialog v-model="tenantDialog" title="新增租户" width="420px">
      <el-form :model="tenantForm" label-width="70px">
        <el-form-item label="编码"><el-input v-model="tenantForm.code" placeholder="唯一编码，如 acme" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="tenantForm.name" placeholder="租户名称" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tenantDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onCreateTenant">创建</el-button>
      </template>
    </el-dialog>

    <!-- 修改我的密码 -->
    <el-dialog v-model="selfPwdVisible" title="修改我的密码" width="420px">
      <el-form label-width="80px">
        <el-form-item label="原密码"><el-input v-model="selfPwd.old" type="password" show-password /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="selfPwd.next" type="password" show-password /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="selfPwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSelfPwd">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Key, Plus, Refresh } from '@element-plus/icons-vue'
import {
  store,
  loadAdminMeta,
  setUserTenantFilter,
  createAdminUser,
  updateAdminUser,
  removeAdminUser,
  resetAdminUserPassword,
  createAdminTenant,
  updateAdminTenant,
  removeAdminTenant,
  createAdminRole,
  updateAdminRole,
  removeAdminRole,
  fetchAdminPermissionCatalog,
  changeOwnPassword
} from '../store'

const tab = ref('users')
const saving = ref(false)
const userDialog = ref(false)
const resetDialog = ref(false)
const tenantDialog = ref(false)
const selfPwdVisible = ref(false)
const resetTarget = ref(null)
const newPassword = ref('')

const userForm = reactive({ username: '', password: '', display_name: '', tenant_id: '', role: 'viewer' })
const tenantForm = reactive({ code: '', name: '' })
const selfPwd = reactive({ old: '', next: '' })

const ROLE_LABEL = { admin: '管理员', analyst: '分析师', viewer: '只读用户' }
const ROLE_TAG = { admin: 'danger', analyst: 'warning', viewer: 'info' }
const roleLabel = (r) => ROLE_LABEL[r] || r
const roleTag = (r) => ROLE_TAG[r] || 'info'

function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}

function openCreateUser() {
  Object.assign(userForm, {
    username: '', password: '', display_name: '',
    tenant_id: store.admin.tenants[0]?.id || '', role: 'viewer'
  })
  userDialog.value = true
}
function openResetPwd(row) {
  resetTarget.value = row
  newPassword.value = ''
  resetDialog.value = true
}

async function onCreateUser() {
  if (!userForm.username || !userForm.password || !userForm.tenant_id) {
    ElMessage.warning('用户名 / 密码 / 租户必填')
    return
  }
  saving.value = true
  try {
    await createAdminUser({ ...userForm })
    ElMessage.success('用户已创建')
    userDialog.value = false
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    saving.value = false
  }
}

async function toggleStatus(row) {
  try {
    await updateAdminUser(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
    ElMessage.success('已更新')
  } catch (e) { ElMessage.error(e.message || '更新失败') }
}

async function onDeleteUser(row) {
  const ok = await ElMessageBox.confirm(`删除用户「${row.username}」？`, '删除确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
  }).catch(() => null)
  if (!ok) return
  try {
    await removeAdminUser(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

async function onResetPwd() {
  if (!newPassword.value || newPassword.value.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  saving.value = true
  try {
    await resetAdminUserPassword(resetTarget.value.id, newPassword.value)
    ElMessage.success('密码已重置')
    resetDialog.value = false
  } catch (e) {
    ElMessage.error(e.message || '重置失败')
  } finally {
    saving.value = false
  }
}

async function onCreateTenant() {
  if (!tenantForm.code || !tenantForm.name) {
    ElMessage.warning('编码与名称必填')
    return
  }
  saving.value = true
  try {
    await createAdminTenant({ code: tenantForm.code.trim(), name: tenantForm.name.trim() })
    ElMessage.success('租户已创建')
    tenantDialog.value = false
    tenantForm.code = ''
    tenantForm.name = ''
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    saving.value = false
  }
}

async function toggleTenant(row) {
  try {
    await updateAdminTenant(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
    ElMessage.success('已更新')
  } catch (e) { ElMessage.error(e.message || '更新失败') }
}

async function onDeleteTenant(row) {
  const ok = await ElMessageBox.confirm(`删除租户「${row.name}」？`, '删除确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
  }).catch(() => null)
  if (!ok) return
  try {
    await removeAdminTenant(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

async function onSelfPwd() {
  if (!selfPwd.old || !selfPwd.next) {
    ElMessage.warning('请填写原密码与新密码')
    return
  }
  saving.value = true
  try {
    await changeOwnPassword(selfPwd.old, selfPwd.next)
    ElMessage.success('密码已修改')
    selfPwdVisible.value = false
    selfPwd.old = ''
    selfPwd.next = ''
  } catch (e) {
    ElMessage.error(e.message || '修改失败')
  } finally {
    saving.value = false
  }
}

/* ===== 角色管理：自定义角色增删改 + 权限点勾选 ===== */
const roleDialog = ref(false)
const roleEditing = ref(false)
const permissionCatalog = ref([])
const roleForm = reactive({ name: '', label: '', description: '', permissions: [] })

/* 内置角色权限锁定（后端同样拒绝，前端只读提示） */
const rolePermLocked = computed(() => {
  const editing = store.admin.roles.find((r) => r.name === roleForm.name)
  return roleEditing.value && editing && editing.is_builtin
})

/* 权限点按 读/写/管理 分组展示（:write 后缀与 admin 归"写与管理"） */
const permissionGroups = computed(() => {
  const all = permissionCatalog.value
  if (!all.length) return []
  const read = all.filter((p) => p !== 'admin' && !p.endsWith(':write'))
  const write = all.filter((p) => p === 'admin' || p.endsWith(':write'))
  return [
    { label: '只读能力', items: read },
    { label: '写与管理能力', items: write },
  ]
})

function togglePermGroup(group) {
  const set = new Set(roleForm.permissions)
  const allOn = group.items.every((p) => set.has(p))
  for (const p of group.items) {
    if (allOn) set.delete(p)
    else set.add(p)
  }
  roleForm.permissions = [...set]
}

async function ensurePermissionCatalog() {
  if (!permissionCatalog.value.length) {
    try {
      permissionCatalog.value = (await fetchAdminPermissionCatalog()) || []
    } catch (e) {
      /* 失败时对话框展示重试入口，不静默空白 */
    }
  }
}

async function reloadPermissionCatalog() {
  permissionCatalog.value = []
  await ensurePermissionCatalog()
  if (!permissionCatalog.value.length) {
    ElMessage.error('权限点清单拉取失败，请确认后端已重启加载角色管理接口')
  }
}

async function openCreateRole() {
  await ensurePermissionCatalog()
  roleEditing.value = false
  Object.assign(roleForm, { name: '', label: '', description: '', permissions: ['chat'] })
  roleDialog.value = true
}

async function openEditRole(row) {
  await ensurePermissionCatalog()
  roleEditing.value = true
  Object.assign(roleForm, {
    name: row.name,
    label: row.label || '',
    description: row.description || '',
    permissions: [...(row.permissions || [])],
  })
  roleDialog.value = true
}

async function onSaveRole() {
  if (!roleForm.name || !roleForm.label) {
    ElMessage.warning('标识与显示名必填')
    return
  }
  saving.value = true
  try {
    if (roleEditing.value) {
      const body = { label: roleForm.label, description: roleForm.description }
      if (!rolePermLocked.value) body.permissions = roleForm.permissions
      await updateAdminRole(roleForm.name, body)
      ElMessage.success('角色已保存')
    } else {
      await createAdminRole({ ...roleForm })
      ElMessage.success('角色已创建')
    }
    roleDialog.value = false
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggleRole(row) {
  try {
    await updateAdminRole(row.name, { status: row.status === 'active' ? 'disabled' : 'active' })
    ElMessage.success('已更新')
  } catch (e) { ElMessage.error(e.message || '更新失败') }
}

async function onDeleteRole(row) {
  const ok = await ElMessageBox.confirm(
    `删除角色「${row.label || row.name}」？仍被用户使用时会被拒绝。`,
    '删除确认', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await removeAdminRole(row.name)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.users-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.users-page > * { flex: none; }
.um-head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 6px; }
.um-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.um-sub { font-size: 12px; color: #8a9099; margin-top: 4px; }
.um-head-actions { display: flex; gap: 8px; }
.um-alert { margin: 8px 0; }
.um-tabs { margin-top: 4px; }
.um-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.um-hint { font-size: 12px; color: #8a9099; }
.perm-tag { margin: 0 6px 6px 0; }
/* 角色编辑：权限点分组勾选 */
.perm-groups { width: 100%; display: flex; flex-direction: column; gap: 8px; }
.perm-group { border: 1px solid var(--el-border-color-lighter, #e4e7ed); border-radius: 8px; padding: 8px 12px; }
.perm-group-title { font-size: 12px; color: #8a9099; margin-bottom: 4px; display: flex; align-items: center; justify-content: space-between; }
.perm-group .el-checkbox { margin-right: 14px; }
</style>
