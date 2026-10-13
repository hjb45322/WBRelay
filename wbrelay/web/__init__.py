# -*- coding: utf-8 -*-
"""HTTP 服务子包：server（骨架与路由分派）+ 各 API 分组 mixin。

兼容旧引用：``wbrelay.web.Handler`` 仍可直接导入（转口自 server）。
"""
from .server import Handler  # noqa: F401
