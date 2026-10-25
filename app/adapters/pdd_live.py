"""拼多多商家后台。登录和报表都走本机浏览器里的官方页面。

没有稳定的公开推广 API。这里不拼签名、不绕过验证码，只在店主扫码后的
会话里打开报表页，收下页面自己加载或下载的数据。字段对不上时见
`pdd_normalize.py`：宁可提示没识别到，也不编造数字。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.adapters.base import SyncPayload
from app.adapters.pdd_session import storage_state_path
from app.errors import AppError

_SCAN = "请先扫码登录真实店铺。打开登录页后，用拼多多商家版扫描二维码。"


class PddLiveAdapter:
    mode = "live"

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def fetch(self, *, today: date, now: datetime) -> SyncPayload:
        del now
        if not storage_state_path(self.db_path).is_file():
            raise AppError(_SCAN, status=409, code="login_required")
        from app.services.live_login import acquire_browser, current_driver, release_browser

        driver = current_driver()
        driver.check_ready()
        if not acquire_browser(0):
            raise AppError("浏览器正忙，请稍后再试", status=409, code="browser_busy")
        try:
            return driver.collect(self.db_path, today=today)
        finally:
            release_browser()
