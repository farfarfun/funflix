"""注册邀请码。

自助注册（`POST /auth/register`）必须凭一张码才能换到账号，换出来的一律是
`UserRole.GUEST`。码由 `funflix invite create` 签发。

为什么不是配置里一个共享口令：共享口令一旦外传就只能整体更换，换了所有人都得
重新拿；也看不出被谁用过几次。这张表能限次、限期、单独吊销。

列定义和签发/消耗/吊销的逻辑都在 funauth 里（`InviteCodeMixin` 与
`Accounts.consume_invite`），这里只把它落成本仓 `Base` 上的一张具体表。
"""

from __future__ import annotations

import sqlalchemy as sa
from funauth import InviteCodeMixin

from funflix.models.base import Base, TimestampMixin


class InviteCode(InviteCodeMixin, TimestampMixin, Base):
    """一张邀请码：码值 + 可用次数 + 过期时间 + 启用标记。

    「还能不能用」是三个条件的合取，判定只许写在
    `funauth.Accounts.consume_invite` 的那条条件 UPDATE 里 —— 在别处重新拼一遍
    等于埋一个会和它悄悄分叉的副本。

    唯一约束留在本仓，理由同 `User`。
    """

    __tablename__ = "invite_code"

    __table_args__ = (sa.UniqueConstraint("code", name="uq_invite_code_code"),)
