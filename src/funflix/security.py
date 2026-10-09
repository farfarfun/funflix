"""密码哈希。

实现已经搬进 funauth（和登录逻辑放在一起）。这里保留模块只是为了不打断现有的
`from funflix.security import hash_password` —— 迁移脚本、CLI、funflix-api 和
测试都在用这条 import，而落库的哈希格式不许有第二套实现：同一个库里两份
bcrypt 封装，哪天一边改了 cost 另一边不知道，是那种查起来很费劲的不一致。
"""

from __future__ import annotations

from funauth import hash_password, verify_password

__all__ = ["hash_password", "verify_password"]
