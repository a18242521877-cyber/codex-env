"""推广报表捕获：只留地址和报表正文，不访问拼多多。"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from app.adapters.pdd_browser import (
    DATE_LABELS,
    MMS_HOME_URL,
    _take_response,
    body_looks_like_mikasa,
    bucket_has_metrics,
    default_report_urls,
    guidance_note,
    is_date_preset_label,
    is_promo_nav_label,
    is_promo_response_url,
    promo_sso_url,
    split_response_url,
)
from app.adapters.pdd_normalize import normalize_captures
from app.config import OFFICIAL_PROMO_URL
import unittest


class _Response:
    def __init__(self, url: str, payload, status: int = 200, content_type: str = "application/json"):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._payload = payload

    def json(self):
        return self._payload

    def body(self):
        return b""


class HarvestSelectionTests(unittest.TestCase):
    def test_sso_entry_points_at_the_merchant_jump(self):
        urls = default_report_urls()
        self.assertEqual(urls[0], MMS_HOME_URL)
        jump = promo_sso_url(OFFICIAL_PROMO_URL)
        self.assertIn(jump, urls)
        self.assertIn("accessType=auto", jump)
        self.assertIn("platform=yingxiao", jump)
        self.assertIn(quote(OFFICIAL_PROMO_URL, safe=""), jump)
        self.assertTrue(jump.startswith("https://mms.pinduoduo.com/login/sso?"))

    def test_labels_do_not_include_generate_report(self):
        self.assertTrue(is_promo_nav_label("商品推广"))
        self.assertTrue(is_promo_nav_label("推广中心"))
        self.assertFalse(is_promo_nav_label("生成报表"))
        self.assertFalse(is_promo_nav_label("开启推广"))
        self.assertTrue(is_date_preset_label("近7天"))
        self.assertIn("近7天", DATE_LABELS)
        self.assertFalse(is_date_preset_label("生成报表"))

    def test_capture_keeps_path_and_drops_query_token(self):
        base, path = split_response_url("https://yingxiao.pinduoduo.com/api/goods/promotion/list.json?access_token=should-not-store")
        self.assertEqual(path, "/api/goods/promotion/list.json")
        self.assertNotIn("should-not-store", base)
        self.assertTrue(is_promo_response_url(base))
        self.assertFalse(is_promo_response_url("https://mms.pinduoduo.com/janus/api/userinfo"))

    def test_mikasa_null_is_not_a_report(self):
        profile = {"mall_id": 753391861, "mall_name": "御品渔获专营店", "mikasaErrorCode": 54001, "result": None}
        self.assertTrue(body_looks_like_mikasa(profile))
        self.assertFalse(body_looks_like_mikasa({"mikasaErrorCode": 1, "result": {"list": [{"spend": 1}]}}))
        bucket = [{"kind": "json", "promo": False, "body": profile}]
        seen = [{"url": "https://mms.pinduoduo.com/api/mms/profile.json", "path": "/api/mms/profile.json", "promo": False}]
        self.assertFalse(bucket_has_metrics(bucket))
        self.assertIn("mikasa", guidance_note(bucket, seen))
        self.assertIn("推广中心", guidance_note(bucket, seen))

    def test_noise_does_not_evict_the_promo_body(self):
        bucket: list[dict] = []
        seen: list[dict] = []
        for index in range(40):
            _take_response(
                _Response(f"https://mms.pinduoduo.com/api/goods/noise/{index}.json", {"ok": index}),
                bucket,
                seen,
            )
        _take_response(
            _Response(
                "https://yingxiao.pinduoduo.com/mains/account/report.json?access_token=should-not-store",
                {
                    "mallId": 753391861,
                    "mallName": "御品渔获专营店",
                    "result": {
                        "list": [
                            {
                                "statDate": "2026-10-24",
                                "goodsId": 1,
                                "goodsName": "渔获单品",
                                "impression": 10,
                                "click": 1,
                                "spend": 3.5,
                                "gmv": 8,
                                "orderNum": 1,
                            }
                        ]
                    },
                },
            ),
            bucket,
            seen,
        )
        _take_response(
            _Response("https://mms.pinduoduo.com/api/mms/profile.json", {"mall_id": 753391861, "result": None, "mikasaErrorCode": 1}),
            bucket,
            seen,
        )
        urls = [item.get("url", "") for item in bucket]
        self.assertTrue(any(url.endswith("/mains/account/report.json") for url in urls))
        self.assertTrue(all("should-not-store" not in url for url in urls))
        self.assertTrue(any(item["path"] == "/api/mms/profile.json" for item in seen))
        payload = normalize_captures(bucket, shop_hint="御品渔获专营店", today=date(2026, 10, 25))
        self.assertEqual(payload.shop.shop_id, "live-753391861")
        self.assertEqual([item.name for item in payload.goods], ["渔获单品"])
        days = [day for rows in payload.goods_days.values() for day in rows]
        self.assertEqual(days[0].spend_fen, 350)

    def test_note_is_kept_when_resume_reads_capture(self):
        payload = normalize_captures(
            [
                {"kind": "debug", "urls": [{"path": "/api/mms/profile.json"}]},
                {"kind": "note", "body": "已登录商家后台，但没有收到推广报表请求。"},
                {"kind": "json", "url": "https://mms.pinduoduo.com/api/mms/profile.json", "path": "/api/mms/profile.json", "body": {"mall_id": 7, "mall_name": "空店"}},
            ],
            today=date(2026, 10, 25),
        )
        self.assertEqual(payload.goods, [])
        self.assertIn("没有收到推广报表请求", payload.note)
        self.assertEqual(payload.shop.name, "空店")
