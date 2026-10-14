# -*- coding: utf-8 -*-
"""版本更新检测 API：GET /api/update（只读；默认只有用户点按钮才会被调用）。"""
from ..update import check


class ApiUpdateMixin:
    def _api_update(self):
        """检查是否有新版本（结构见 wbrelay.update.check）。

        边界（docs/方案-版本更新检测.md）：
          * 只发一次 GET、不传任何本机信息、5s 超时；
          * 失败静默降级，绝不抛给调用方，也绝不影响网关功能；
          * **只读模式同样可用**：结果不落盘，不写任何文件。
        """
        return self._json(200, check())
