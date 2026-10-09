"""账号域的**绑定**是否接对了。

登录校验、角色判定、邀请码原子消耗这些行为本身由
[funauth](https://github.com/farfarfun/funauth) 自己的测试覆盖（包括那条「消耗
必须是一条带三个守卫的 UPDATE」的语句断言）—— 在这里再抄一遍只会得到两份会
分叉的副本。

本仓要验的是另一类风险：mixin 落到我们自己的 `Base` 上之后，表名、约束名、列
是不是都在；`accounts` 这个实例绑的是不是我们的两个模型类；以及一条端到端的
真路径（建 admin → 登录 → 签码 → 凭码注册出 guest）能在本仓的 session 夹具上
跑通。这几样坏掉的方式和 funauth 内部的 bug 完全不同，只能在这边测。
"""

from __future__ import annotations

import pytest
from funauth import BadCredentials, InviteUnusable, UserRole
from funauth import security as _funauth_security

from funflix import security as _funflix_security
from funflix.models import InviteCode, User
from funflix.services.account import accounts


class TestBinding:
    def test_accounts_is_bound_to_our_models(self) -> None:
        """绑错模型类的话所有查询都会打到别的表上，而且不报错。"""
        assert accounts.user_model is User
        assert accounts.invite_model is InviteCode

    def test_security_module_is_a_re_export(self) -> None:
        """`funflix.security` 必须就是 funauth 那两个函数本身。

        哪天有人在这边重新实现一遍，落库的哈希就有了两套来源 —— 两边 cost 不同
        时谁都不会报错，只是密码校验开始忽快忽慢，查起来很费劲。
        """
        assert _funflix_security.hash_password is _funauth_security.hash_password
        assert _funflix_security.verify_password is _funauth_security.verify_password

    def test_user_table_shape(self) -> None:
        """mixin 的列有没有真的落到我们的 Base 上，约束名有没有变。

        约束名会进迁移、进 `ON CONFLICT`，生产库里已经叫 `uq_user_username` 了，
        funauth 升级不该把它改掉。
        """
        table = User.__table__
        assert table.name == "user"
        assert {"id", "username", "password_hash", "role", "is_active"} <= set(table.columns.keys())
        # TimestampMixin 用的是本仓那份，不是 funauth 自带的
        assert {"created_at", "updated_at"} <= set(table.columns.keys())
        assert {c.name for c in table.constraints if c.name} >= {"uq_user_username"}

    def test_invite_table_shape(self) -> None:
        table = InviteCode.__table__
        assert table.name == "invite_code"
        assert {
            "id",
            "code",
            "max_uses",
            "used_count",
            "expires_at",
            "is_active",
            "note",
            "created_at",
            "updated_at",
        } <= set(table.columns.keys())
        assert {c.name for c in table.constraints if c.name} >= {"uq_invite_code_code"}

    def test_role_column_default_is_guest(self) -> None:
        """漏传角色时往最小权限掉，而不是凭空多一个管理员。"""
        assert User.__table__.c.role.default.arg is UserRole.GUEST


class TestEndToEnd:
    """走一遍真路径，确认模型 + funauth + 本仓 session 夹具串得起来。"""

    @pytest.mark.asyncio
    async def test_create_admin_then_login(self, session) -> None:
        user = await accounts.create_user(session, "boss", "pw", UserRole.ADMIN)
        assert user.role is UserRole.ADMIN

        assert await accounts.authenticate(session, "boss", "pw")
        with pytest.raises(BadCredentials):
            await accounts.authenticate(session, "boss", "wrong")

    @pytest.mark.asyncio
    async def test_issue_code_then_register_guest(self, session) -> None:
        code = await accounts.issue_invite(session, max_uses=1, expires_in_days=7, note="给张三")
        assert accounts.describe_invite_status(code) == "可用"

        user = await accounts.register_with_invite(session, "newbie", "pw", code.code)
        assert user.role is UserRole.GUEST
        assert await accounts.authenticate(session, "newbie", "pw")

        # 一次性码不能兑第二个账号
        with pytest.raises(InviteUnusable):
            await accounts.register_with_invite(session, "another", "pw", code.code)
        assert await accounts.get_by_username(session, "another") is None

    @pytest.mark.asyncio
    async def test_revoke_then_unusable(self, session) -> None:
        code = await accounts.issue_invite(session)

        assert await accounts.revoke_invite(session, code.code) is True
        with pytest.raises(InviteUnusable):
            await accounts.register_with_invite(session, "nope", "pw", code.code)

    @pytest.mark.asyncio
    async def test_disable_blocks_login(self, session) -> None:
        await accounts.create_user(session, "u", "pw", UserRole.GUEST)

        assert await accounts.set_active(session, "u", False) is True
        with pytest.raises(BadCredentials):
            await accounts.authenticate(session, "u", "pw")

    @pytest.mark.asyncio
    async def test_list_users_shows_both_roles(self, session) -> None:
        await accounts.create_user(session, "zoe", "pw", UserRole.GUEST)
        await accounts.create_user(session, "adam", "pw", UserRole.ADMIN)

        users = await accounts.list_users(session)
        assert [(u.username, u.role) for u in users] == [
            ("adam", UserRole.ADMIN),
            ("zoe", UserRole.GUEST),
        ]
