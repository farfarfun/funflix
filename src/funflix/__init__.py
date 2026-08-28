"""funflix —— 影视资源分享文本的结构化采集、解析与网盘链接校验。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("funflix")
except PackageNotFoundError:
    __version__ = "0.0.0"
