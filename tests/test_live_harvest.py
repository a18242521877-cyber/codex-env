"""推广报表捕获：只留地址和报表正文，不访问拼多多。"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from app.adapters.pdd_browser import (
    DATE_LABELS,
    GOODS_EFFECT_URL,
    MMS_HOME_URL,
    SIDEBAR_TARGETS,
    SSO_STUCK_NOTE,
    _take_response,
    body_looks_like_mikasa,
    bucket_has_metrics,
    capture_has_sydney,
    default_report_urls,
    guidance_note,
    is_date_preset_label,
    is_promo_nav_label,
    is_promo_response_url,
    promo_sso_url,
    split_response_url,
)
from app.adapters.pdd_normalize import normalize_captures
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
        self.assertIn(GOODS_EFFECT_URL, urls)
        for target in SIDEBAR_TARGETS:
            jump = promo_sso_url(target)
            self.assertIn(jump, urls)
            self.assertIn("accessType=auto", jump)
            self.assertIn("platform=yingxiao", jump)
            self.assertIn(quote(target, safe=""), jump)
            self.assertTrue(jump.startswith("https://mms.pinduoduo.com/login/sso?"))
        joined = "".join(urls)
        self.assertIn(quote("https://yingxiao.pinduoduo.com/mains/promotionOverview", safe=""), joined)
        self.assertIn(quote("https://yingxiao.pinduoduo.com/goods/promotion/list", safe=""), joined)
        self.assertIn(quote("https://yingxiao.pinduoduo.com/goods/report/promotion/overView", safe=""), joined)
        self.assertIn(quote("https://yingxiao.pinduoduo.com/mains/account/finance", safe=""), joined)

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
        self.assertTrue(is_promo_response_url("https://yingxiao.pinduoduo.com/mms-gateway/venus/api/homepage/query"))
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

    def test_sydney_yesdata_fills_yesterday_without_calling_it_promo_spend(self):
        today = date(2026, 10, 26)
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "path": "/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "body": {
                        "success": True,
                        "result": {
                            "mallId": 753391861,
                            "mallName": "御品渔获专营店",
                            "yesData": {
                                "payOrdrAmt": "5538.6",
                                "payOrdrCnt": "122",
                                "payUvRto": "6.23%",
                                "gpv": "2722",
                                "guv": "1953",
                                "vstGoodsCnt": "7",
                                "payOrdrUsrCnt": "122",
                            },
                            "todayData": {"payOrdrAmt": "12.5", "payOrdrCnt": "1", "gpv": "20"},
                        },
                    },
                },
                {"kind": "note", "body": SSO_STUCK_NOTE},
            ],
            shop_hint="御品渔获专营店",
            today=today,
        )
        self.assertEqual(payload.shop.shop_id, "live-753391861")
        self.assertEqual(len(payload.goods), 1)
        self.assertEqual(payload.goods[0].name, "全店商品数据")
        yesterday = payload.goods_days[payload.goods[0].goods_id][0]
        self.assertEqual(yesterday.stat_date, "2026-10-25")
        self.assertEqual(yesterday.gmv_fen, 553860)
        self.assertEqual(yesterday.orders, 122)
        self.assertEqual(yesterday.impressions, 2722)
        self.assertEqual(yesterday.spend_fen, 0)
        self.assertIn("商品数据", payload.note)
        self.assertIn("推广花费", payload.note)
        self.assertIn("授权", payload.note)
        self.assertTrue(capture_has_sydney([{"kind": "json", "body": {"result": {"yesData": {}}}}]))
        self.assertEqual(guidance_note([{"kind": "json", "body": {"result": {"yesData": {"payOrdrAmt": "1.0"}}}}], []), "")

    def test_plaintext_yeslist_beats_obfuscated_glyphs_and_shop_total(self):
        today = date(2026, 10, 26)
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "body": {
                        "result": {
                            "yesData": {"payOrdrAmt": "5538.6", "payOrdrCnt": "122", "gpv": "2722"},
                            "yesList": [
                                {
                                    "goodsId": 9001,
                                    "goodsName": "渔获单品",
                                    "payOrdrAmt": "10.5",
                                    "payOrdrCnt": "2",
                                    "gpv": "80",
                                },
                                {
                                    "goodsId": 9002,
                                    "goodsName": "乱码商品",
                                    "payOrdrAmt": "\ue501\ue502",
                                    "payOrdrCnt": "\ue503",
                                    "gpv": "\ue504",
                                },
                            ],
                        }
                    },
                }
            ],
            today=today,
        )
        self.assertEqual([item.name for item in payload.goods], ["渔获单品"])
        day = payload.goods_days[payload.goods[0].goods_id][0]
        self.assertEqual(day.gmv_fen, 1050)
        self.assertEqual(day.orders, 2)
        self.assertEqual(day.spend_fen, 0)
        self.assertNotIn("5538.60", payload.note)

    def test_sso_stuck_without_sydney_is_not_a_silent_empty_sync(self):
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/home/",
                    "body": {"mallId": 753391861, "mallName": "御品渔获专营店", "permissionList": [], "result": None},
                },
                {"kind": "note", "body": SSO_STUCK_NOTE},
            ],
            today=date(2026, 10, 26),
        )
        self.assertEqual(payload.goods, [])
        self.assertEqual(payload.campaigns, [])
        self.assertIn("授权", payload.note)
        self.assertIn("没有写入推广花费", payload.note)
        self.assertNotEqual(payload.note.strip(), "")

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
