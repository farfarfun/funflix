"""账号域：把 funauth 绑到本仓的两张表上。

登录校验、角色判定、邀请码的签发/消耗/吊销、凭码注册 —— 实现全在
[funauth](https://github.com/farfarfun/funauth) 里，本模块只有一行有效代码：
告诉它本仓的 `User` / `InviteCode` 类是哪两个。

## 为什么实现不在这儿

邀请码的原子消耗是个**并发正确性**相关的实现（一条带三个守卫的条件 UPDATE，
先查后改会把一张一次性码兑出两个账号），而 CLI、HTTP 接口、测试三个调用方都要
用它。放在任何一个调用方里，另外两个就只能各自抄一遍。抽到 funauth 之后，它还
能带着自己的 20 个单测走，往后邮箱 / 短信 / QQ / 微信扫码登录也都长在那边。

## 调用方约定

- 本模块只导出 `accounts` 这一个实例，异常类型直接从 `funauth` import
  （`BadCredentials` / `UsernameTaken` / `InviteUnusable` / `PermissionDenied`，
  都是 `RuntimeError` 子类）。不在这里转发一遍是为了让「这些语义来自 funauth」
  在 import 行上就看得见。
- `accounts` 的方法都收一个 `AsyncSession`，事务边界由调用方决定。
- `register_with_invite` 产出的角色恒为 `GUEST`，不接受指定；要建管理员走
  `create_user`（`funflix user create --role admin`）。
"""

from __future__ import annotations

from funauth import Accounts

from funflix.models import InviteCode, User

#: 全局单例。用实例持有宿主模型类而不是模块级 `configure()`：没有可变全局状态，
#: import 顺序无关，测试里想换表再建一个实例就行（理由见 `funauth.AccountsBase`）。
accounts = Accounts(user_model=User, invite_model=InviteCode)

__all__ = ["accounts"]
