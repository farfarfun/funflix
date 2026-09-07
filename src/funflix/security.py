"""密码哈希。

直接用 `bcrypt`，不引入 passlib——passlib 已多年未发版，对 bcrypt 4.x 的
`__about__` 变更没跟上，社区里一堆关于它报 warning/崩溃的 issue。bcrypt
库本身的 API 就两个函数，没有再包一层的必要。
"""

from __future__ import annotations

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 哈希格式不对（比如库迁移时手滑存了明文）：当作校验失败而不是 500
        return False
