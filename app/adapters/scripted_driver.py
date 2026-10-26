"""不访问拼多多的登录夹具。自检用它走完真实模式的接口，而不是弹出浏览器。"""

from __future__ import annotations

from datetime import date, timedelta

from app.adapters.pdd_normalize import normalize_captures
from app.adapters.pdd_session import storage_state_path, write_private_text


def scripted_payload(today: date | None = None):
    today = today or date.today()
    day = (today - timedelta(days=1)).isoformat()
    return normalize_captures(
        [
            {
                "kind": "json",
                "body": {
                    "mallId": 10001,
                    "mallName": "夹具真实店",
                    "list": [
                        {
                            "statDate": day,
                            "goodsId": 9001,
                            "goodsName": "夹具衣架",
                            "scene": "商品推广",
                            "impression": 5000,
                            "click": 100,
                            "spend": 500.0,
                            "gmv": 100.0,
                            "orderNum": 10,
                            "dailyBudget": 1000.0,
                        },
                        {
                            "statDate": day,
                            "creativeId": "a",
                            "creativeName": "主图A",
                            "goodsId": 9001,
                            "impression": 3000,
                            "click": 80,
                            "spend": 20.0,
                            "gmv": 40.0,
                            "orderNum": 2,
                        },
                    ],
                },
            }
        ],
        shop_hint="夹具真实店",
        today=today,
    )


class ScriptedLiveDriver:
    def __init__(self):
        self.opened_url = ""

    def check_ready(self) -> None:
        return None

    def open_url_only(self, db_path, url: str) -> str:
        del db_path
        self.opened_url = url
        return url

    def has_open_session(self) -> bool:
        return False

    def harvest_open(self, db_path, today):
        del db_path, today
        return None

    def close_open_session(self) -> None:
        return None

    def run(self, job, db_path, **kwargs) -> None:
        if job.snapshot().get("cancelled") and not job.snapshot().get("cookies_saved"):
            return
        write_private_text(storage_state_path(db_path), '{"cookies":[],"origins":[]}\n')
        job.update(cookies_saved=True)
        from app.adapters.pdd_normalize import SAVED_LOGIN_NOTE, normalize_captures
        from app.services.live_login import finish_live_session, retain_saved_login

        if kwargs.get("establish_session") is False:
            job.update(status="confirmed", finished=True, cookies_saved=True, message=SAVED_LOGIN_NOTE)
            return
        payload = normalize_captures([], shop_hint="夹具真实店", today=date.today())
        payload.note = SAVED_LOGIN_NOTE
        result = finish_live_session(db_path, payload, job.ticket, generation=getattr(job, "generation", None))
        if result.get("skipped"):
            return
        # 取消发生在写入之后也不能删会话。重新打开登录只停掉还没扫完的那一次。
        retain_saved_login(db_path, job)
        job.update(status="confirmed", finished=True, cookies_saved=True, message=SAVED_LOGIN_NOTE)

    def collect(self, db_path, *, today: date):
        return scripted_payload(today)
