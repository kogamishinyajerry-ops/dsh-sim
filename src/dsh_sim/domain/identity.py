"""开发模式身份解析（定义书 §权限、身份与文件安全；TBD-08）。

⚠️ 生产环境必须替换为受信 IdP 签发的短期代理凭据 + 项目级授权（TBD-08）。
本模块只做开发模式：从请求头 X-Dev-Subject / X-Dev-Roles / X-Dev-Projects 解析身份，
不做密码存储、不做签名校验。无可靠多人身份时只能做单用户演示，不能宣称团队验收成立。

职责分离（FR-24）：
- EXECUTOR：执行工程师；不能自行接受本人任务。
- REVIEWER：指定审查人；决定仅限可信人工会话。
- CAPABILITY_OWNER：能力 Owner；发布受控。
- NODE_ADMIN：节点管理员 / Worker 服务账户。
- AGENT：模型代理；永远不得取得 authorizeRuns / decideReview 等人工批准动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    EXECUTOR = "EXECUTOR"
    REVIEWER = "REVIEWER"
    CAPABILITY_OWNER = "CAPABILITY_OWNER"
    NODE_ADMIN = "NODE_ADMIN"
    AGENT = "AGENT"


@dataclass(frozen=True)
class Identity:
    """请求身份。subject_id 与角色仅来自凭据；payload 中的用户字段只是业务指派请求。"""

    subject_id: str
    roles: frozenset[Role] = field(default_factory=frozenset)
    project_ids: frozenset[str] = field(default_factory=frozenset)
    is_agent: bool = False

    def has_role(self, role: Role) -> bool:
        return role in self.roles

    def can_access_project(self, project_id: str) -> bool:
        """项目级数据隔离（FR-24）：跨项目读取必须被拒。"""
        return project_id in self.project_ids

    def require_human(self) -> None:
        """人工批准动作入口：Agent Bearer 始终拒绝（定义书 §authorizeRuns 强约束）。"""
        from dsh_sim.domain.errors import ApiError, ErrorCode

        if self.is_agent or Role.AGENT in self.roles:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                "Agent 身份不能取得人工批准动作（authorizeRuns/decideReview/confirmIssue/closeIssue）",
                details={"subject_id": self.subject_id},
            )


DEV_HEADER_SUBJECT = "X-Dev-Subject"
DEV_HEADER_ROLES = "X-Dev-Roles"
DEV_HEADER_PROJECTS = "X-Dev-Projects"
DEV_HEADER_PROJECT = "X-Dev-Project"  # 当前活动项目（多项目时指定；缺省取 projects 首项）


def parse_dev_identity(headers: dict[str, str]) -> Identity | None:
    """从开发头解析身份；缺 subject 返回 None（调用方按 401 处理）。

    headers 键大小写不敏感（调用方先统一转小写）。
    """
    subject = headers.get(DEV_HEADER_SUBJECT.lower())
    if not subject:
        return None
    roles = frozenset(
        Role(r.strip())
        for r in headers.get(DEV_HEADER_ROLES.lower(), "").split(",")
        if r.strip()
    )
    projects = frozenset(
        p.strip()
        for p in headers.get(DEV_HEADER_PROJECTS.lower(), "").split(",")
        if p.strip()
    )
    is_agent = Role.AGENT in roles
    return Identity(
        subject_id=subject.strip(),
        roles=roles,
        project_ids=projects,
        is_agent=is_agent,
    )


__all__ = [
    "DEV_HEADER_PROJECT",
    "DEV_HEADER_PROJECTS",
    "DEV_HEADER_ROLES",
    "DEV_HEADER_SUBJECT",
    "Identity",
    "Role",
    "parse_dev_identity",
]
