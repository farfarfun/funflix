"""迁移配置的定位。

装出来的包和源码仓库是两种布局，`funflix db upgrade` 在两种下都得能建库。
原来这里是一句 `Config("alembic.ini")` —— 按当前工作目录找文件，于是：

- 装包场景：migrations/ 和 alembic.ini 根本没进 wheel，
  报 `No 'script_location' key found`，使用方只能再 clone 一份源码建库
- 源码场景：也只在「cwd 恰好是仓库根目录」时才成立

见 GitHub issue #1。
"""

from __future__ import annotations

import pathlib

from funflix.cli import _alembic_config


class TestAlembicConfigResolution:
    def test_script_location_is_absolute(self) -> None:
        """必须是绝对路径 —— 相对路径按 cwd 解析，换个目录就找不到。"""
        location = _alembic_config().get_main_option("script_location")
        assert location, "没有 script_location，alembic 会直接报错"
        assert pathlib.Path(location).is_absolute(), f"script_location 不是绝对路径：{location}"

    def test_migrations_directory_actually_exists(self) -> None:
        location = _alembic_config().get_main_option("script_location")
        path = pathlib.Path(location)
        assert path.is_dir(), f"{path} 不是目录"
        assert (path / "env.py").is_file(), f"{path} 里没有 env.py"

    def test_versions_are_reachable(self) -> None:
        """能找到迁移脚本本身，否则 upgrade 会「成功」但一张表都不建。"""
        location = pathlib.Path(_alembic_config().get_main_option("script_location"))
        versions = list((location / "versions").glob("*.py"))
        assert versions, f"{location}/versions 下没有任何迁移脚本"

    def test_does_not_depend_on_cwd(self, tmp_path, monkeypatch) -> None:
        """换到一个毫不相干的目录，仍然要能定位到迁移。

        这正是装包场景的处境：用户在任何目录敲 `funflix db upgrade`。
        """
        monkeypatch.chdir(tmp_path)
        location = _alembic_config().get_main_option("script_location")
        assert pathlib.Path(location).is_dir(), f"换目录后定位失败：{location}"
