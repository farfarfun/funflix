"""密码哈希。

直接用 `bcrypt`，不引入 passlib——passlib 已多年未发版，对 bcrypt 4.x 的
`__about__` 变更没跟上，社区里一堆关于它报 warning/崩溃的 issue。bcrypt
库本身的 API 就两个函数，没有再包一层的必要。
"""

from __future__ import annotations

import bcrypt


def hash_password(password: str) -> str:
    """对明文密码做 bcrypt 哈希。

    参数：
        password：用户输入的明文密码。

    返回：
        可直接落库的 bcrypt 哈希字符串（含算法标识与随机 salt）。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码是否与已存储的哈希匹配。

    参数：
        password：登录时用户输入的明文密码。
        password_hash：`hash_password` 生成并落库的哈希值。

    返回：
        匹配返回 `True`；不匹配，或 `password_hash` 本身不是合法的 bcrypt
        哈希格式（例如迁移遗留的明文）时返回 `False`。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 哈希格式不对（比如库迁移时手滑存了明文）：当作校验失败而不是 500
        return False
