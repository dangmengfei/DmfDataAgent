"""角色定义（RBAC 基础）。

三级角色：
- admin   管理员：全部功能 + 用户/租户管理
- analyst 分析师：对话、知识库读写、语义查询执行、反馈、观测、记忆管理
- viewer  只读用户：对话、反馈、知识库只读、语义元信息只读

权限串为「能力标签」，两处消费：
1. 前端按 `CurrentUser.permissions` 显隐入口与按钮；
2. 后端以 `require_permission("xxx")` 做**接口级**守卫（admin 始终放行）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from dmf_agent.schemas.auth import RoleInfo

ADMIN = "admin"
ANALYST = "analyst"
VIEWER = "viewer"

ALL_ROLES = (ADMIN, ANALYST, VIEWER)

# ---------- 权限点（能力标签）----------
# 读 / 写 分离：只读能力不给写侧权限点，接口级守卫据此拒绝。
PERM_CHAT = "chat"                      # 对话（含流式）
PERM_KNOWLEDGE = "knowledge"            # 知识库 / 文档 读取
PERM_KNOWLEDGE_WRITE = "knowledge:write"  # 知识库 / 文档 增删改、入库
PERM_SEMANTIC = "semantic"              # 语义层元信息读取（指标 / 维度 / 清单 / 健康）
PERM_SEMANTIC_WRITE = "semantic:write"  # 语义注册中心写（草稿 / 校验 / 发布 / 回滚 / 审批）
PERM_SEMANTIC_QUERY = "semantic:query"  # 语义查询执行（打底表；写侧资源开销）
PERM_FEEDBACK = "feedback"              # 点赞 / 点踩
PERM_OBS = "obs"                        # 运行观测（汇总 / 明细 / 导出）
PERM_MEMORY = "memory"                  # 记忆管理读取（L1 会话摘要 / L2 全平台偏好 / L3 领域知识）
PERM_MEMORY_WRITE = "memory:write"      # 记忆管理写入（L3 领域知识增删改 / 审核、L2 跨用户删除）
PERM_FEWSHOT = "fewshot"                # Few-Shot 样本查看 / 试召回 / 导出
PERM_FEWSHOT_WRITE = "fewshot:write"    # Few-Shot 样本增删改 / 导入
PERM_PLUGIN = "plugin"                  # 插件（Skill / MCP）查看
PERM_PLUGIN_WRITE = "plugin:write"      # 插件注册 / 注销
PERM_PROMPT = "prompt"                  # 系统提示词查看（版本 / 历史 / 内容）
PERM_PROMPT_WRITE = "prompt:write"      # 提示词改版 / 回滚
PERM_SLANG = "slang"                    # 黑话词典查看 / 试改写 / 导出 / 版本
PERM_SLANG_WRITE = "slang:write"        # 黑话词条增删改 / 导入（非 admin 仅限本人用户级）
PERM_SLANG_REVIEW = "slang:review"      # 黑话词条发布 / 停用 / 回滚（非 admin 仅限本人用户级）
PERM_EXPORT_APPROVE = "export:approve"  # FR13 导出放行（security.export_require_approval=True 时导出端点额外要求）
PERM_MODEL = "model"                    # 模型管理查看 / 连接测试
PERM_MODEL_WRITE = "model:write"        # 模型配置增删改 / 设为默认 / 试连
PERM_ADMIN = "admin"                    # 用户 / 租户等平台管理

# 全部权限点（供前端展示与校验；不含通配符 "*"）
ALL_PERMISSIONS: List[str] = [
    PERM_CHAT,
    PERM_KNOWLEDGE,
    PERM_KNOWLEDGE_WRITE,
    PERM_SEMANTIC,
    PERM_SEMANTIC_QUERY,
    PERM_FEEDBACK,
    PERM_OBS,
    PERM_MEMORY,
    PERM_MEMORY_WRITE,
    PERM_FEWSHOT,
    PERM_FEWSHOT_WRITE,
    PERM_PLUGIN,
    PERM_PLUGIN_WRITE,
    PERM_PROMPT,
    PERM_PROMPT_WRITE,
    PERM_SLANG,
    PERM_SLANG_WRITE,
    PERM_SLANG_REVIEW,
    PERM_EXPORT_APPROVE,
    PERM_MODEL,
    PERM_MODEL_WRITE,
    PERM_ADMIN,
]

_WILDCARD = "*"


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    description: str
    permissions: List[str]


_ROLES: Dict[str, Role] = {
    ADMIN: Role(
        name=ADMIN,
        label="管理员",
        description="全部功能，含用户 / 租户 / 平台配置管理",
        permissions=[_WILDCARD],
    ),
    ANALYST: Role(
        name=ANALYST,
        label="分析师",
        description="对话、知识库读写、语义查询、反馈、观测、记忆管理、Few-Shot 样本维护、黑话词典（仅本人用户级可写）；插件与提示词可查看不可改",
        permissions=[
            PERM_CHAT,
            PERM_KNOWLEDGE,
            PERM_KNOWLEDGE_WRITE,
            PERM_SEMANTIC,
            PERM_SEMANTIC_QUERY,
            PERM_FEEDBACK,
            PERM_OBS,
            PERM_MEMORY,
            PERM_FEWSHOT,
            PERM_FEWSHOT_WRITE,
            PERM_PLUGIN,
            PERM_PROMPT,
            PERM_SLANG,
            PERM_SLANG_WRITE,
            PERM_SLANG_REVIEW,
            PERM_SEMANTIC_WRITE,
        ],
    ),
    VIEWER: Role(
        name=VIEWER,
        label="只读用户",
        description="对话与反馈；知识库 / 语义元信息只读，不可写",
        permissions=[
            PERM_CHAT,
            PERM_KNOWLEDGE,
            PERM_SEMANTIC,
            PERM_FEEDBACK,
        ],
    ),
}


def is_valid_role(name: str) -> bool:
    return name in _ROLES


def role_permissions(name: str) -> List[str]:
    role = _ROLES.get(name)
    return list(role.permissions) if role else []


def role_has_permission(role: str, permission: str) -> bool:
    """角色是否具备某权限点；admin（`*`）对任何权限点放行。"""
    perms = role_permissions(role)
    return _WILDCARD in perms or permission in perms


def role_has_any_permission(role: str, permissions: List[str]) -> bool:
    """命中任一权限点即通过（路由可传多个候选能力）。

    ⚠️ 仅按**静态**角色定义判定，不含 DB 中新增/调整过的权限。接口级守卫请用
    `permissions_grant(effective_permissions(...), wanted)`，否则自定义角色
    （管理面新建、不在 `_ROLES` 里）永远过不了守卫。
    """
    return any(role_has_permission(role, p) for p in permissions)


def permissions_grant(permissions: Optional[List[str]], wanted: List[str]) -> bool:
    """给定权限集合是否命中任一目标权限点（`*` 通配）。

    这是**唯一**的权限判定口径：接口守卫、导出闸门、前端 `hasPerm` 都以
    「当前身份携带的 permissions」为准，从而让管理面新建的自定义角色真正生效。
    """
    perms = set(permissions or ())
    if _WILDCARD in perms:
        return True
    return any(p in perms for p in wanted)


def effective_permissions(role: str, permissions: Optional[List[str]]) -> List[str]:
    """DB 权限优先；缺失 / 为空时回落静态角色定义。

    与 `get_current_user` 的取值口径保持一致（`role_permissions_from` 已做
    DB→静态降级）。守卫拿不到 permissions（如测试手工构造的 CurrentUser、
    鉴权关闭时的合成用户）时仍能按角色名兜底，不会出现"权限凭空消失"。
    """
    if permissions:
        return list(permissions)
    return role_permissions(role)


def list_permissions() -> List[str]:
    return list(ALL_PERMISSIONS)


def list_roles() -> List[RoleInfo]:
    return [
        RoleInfo(
            name=r.name,
            label=r.label,
            description=r.description,
            permissions=r.permissions,
        )
        for r in _ROLES.values()
    ]


def builtin_role_specs():
    """内置三角色的持久化种子（label/description/permissions/is_builtin）。"""
    return [
        (r.name, r.label, r.description, list(r.permissions), True)
        for r in _ROLES.values()
    ]


async def seed_builtin_roles(repo) -> int:
    """幂等导入内置三角色（已存在则跳过，绝不覆盖管理面后续可能的改名）。返回新增条数。"""
    created = 0
    from dmf_agent.schemas.auth import RoleData  # 局部导入避免循环

    for name, label, description, permissions, is_builtin in builtin_role_specs():
        if await repo.get(name) is None:
            await repo.add(
                RoleData(
                    name=name, label=label, description=description,
                    permissions=permissions, status="active", is_builtin=is_builtin,
                )
            )
            created += 1
    return created


async def role_permissions_from(repo, name: str) -> Optional[List[str]]:
    """从仓储读角色权限；仓储不可用 / 角色缺失 / 已禁用时回退静态定义（降级矩阵）。

    返回 None 表示该角色在库中显式禁用（调用方据此拒绝登录）；
    返回权限列表（可能为空）表示可正常鉴权。
    """
    if repo is None:
        return role_permissions(name)
    try:
        role = await repo.get(name)
    except Exception:  # noqa: BLE001 - DB 异常回落静态，保证鉴权不被存储故障拖垮
        return role_permissions(name)
    if role is None:
        return role_permissions(name)
    if role.status == "disabled":
        return None
    return list(role.permissions)


def is_builtin_role_name(name: str) -> bool:
    return name in _ROLES


def can_write_knowledge(role: str) -> bool:
    """知识库写权限：admin / analyst。"""
    return role_has_permission(role, PERM_KNOWLEDGE_WRITE)
