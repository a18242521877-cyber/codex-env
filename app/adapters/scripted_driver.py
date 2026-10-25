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
    def check_ready(self) -> None:
        return None

    def run(self, job, db_path) -> None:
        if job.snapshot().get("cancelled"):
            return
        write_private_text(storage_state_path(db_path), '{"cookies":[],"origins":[]}\n')
        payload = scripted_payload()
        from app.services.live_login import finish_live_session

        result = finish_live_session(db_path, payload, job.ticket)
        if job.snapshot().get("cancelled"):
            from app.services.live_login import drop_live_login

            drop_live_login(db_path)
            return
        job.update(status="confirmed", message=result["message"])

    def collect(self, db_path, *, today: date):
        return scripted_payload(today)
