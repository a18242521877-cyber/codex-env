"""推广报表捕获：只留地址和报表正文，不访问拼多多。"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

import json
import tempfile
from pathlib import Path

from app.adapters.pdd_browser import (
    DATE_LABELS,
    DIMENSION_LABELS,
    GOODS_EFFECT_URL,
    MMS_HOME_URL,
    SIDEBAR_TARGETS,
    SSO_STUCK_NOTE,
    _save_capture,
    _take_response,
    body_looks_like_mikasa,
    bucket_has_metrics,
    capture_has_sydney,
    default_report_urls,
    guidance_note,
    is_date_preset_label,
    is_dimension_label,
    is_promo_nav_label,
    is_promo_response_url,
    promo_session_ready,
    promo_sso_url,
    redirect_target,
    split_response_url,
)
from app.adapters.pdd_normalize import (
    body_token_invalid,
    json_has_metrics,
    normalize_captures,
    parse_home_promo_spend_text,
)
from app.adapters.pdd_session import capture_path
from app.services.sync import describe_sync
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
    def test_sync_opens_fixed_targets_without_sso_automation(self):
        urls = default_report_urls()
        self.assertEqual(urls[0], MMS_HOME_URL)
        self.assertIn(GOODS_EFFECT_URL, urls)
        for target in SIDEBAR_TARGETS:
            self.assertIn(target, urls)
        joined = "".join(urls)
        self.assertNotIn("accessType=auto", joined)
        self.assertNotIn("/login/sso", joined)
        jump = promo_sso_url(SIDEBAR_TARGETS[1])
        self.assertIn("accessType=auto", jump)
        self.assertIn(quote(SIDEBAR_TARGETS[1], safe=""), jump)
        self.assertNotIn(jump, urls)

    def test_labels_do_not_include_generate_report(self):
        self.assertTrue(is_promo_nav_label("商品推广"))
        self.assertTrue(is_promo_nav_label("推广中心"))
        self.assertFalse(is_promo_nav_label("生成报表"))
        self.assertFalse(is_promo_nav_label("开启推广"))
        self.assertTrue(is_date_preset_label("近7天"))
        self.assertIn("近7天", DATE_LABELS)
        self.assertFalse(is_date_preset_label("生成报表"))
        self.assertTrue(is_dimension_label("创意"))
        self.assertTrue(is_dimension_label("主图"))
        self.assertIn("创意", DIMENSION_LABELS)
        self.assertFalse(is_dimension_label("生成报表"))
        self.assertFalse(is_dimension_label("开启推广"))

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
        note = guidance_note(bucket, seen)
        self.assertIn("登录态为空", note)
        self.assertIn("测图测款未写入", note)
        self.assertIn("推广中心", note)

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
        self.assertIn("自己完成登录", payload.note)
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
        self.assertIn("自己完成登录", payload.note)
        self.assertIn("没有写入推广花费", payload.note)
        self.assertIn("测图测款未写入", payload.note)
        self.assertNotIn("没有从报表里识别到", payload.note)
        message = describe_sync(payload, {"campaigns": 0, "goods": 0, "images": 0, "days": 0})
        self.assertNotIn("测款 0", message)
        self.assertNotIn("已同步", message)
        self.assertIn("登录态为空", message)

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

    def test_help_text_spend_is_not_a_promo_report(self):
        config = {
            "domains": ["https://yingxiao.pinduoduo.com/mains/promotionOverview"],
            "help": "这里解释总花费、消耗和推广花费怎么看，不是一行商品数据",
            "mallId": 753391861,
        }
        self.assertFalse(json_has_metrics(config))
        bucket = [{"kind": "json", "promo": True, "url": "https://mms.pinduoduo.com/api/config", "body": config}]
        self.assertFalse(bucket_has_metrics(bucket))
        auth = {"errorMsg": "登录态为空", "mikasaErrorCode": 10, "result": None}
        payload = normalize_captures(
            [
                {"kind": "json", "url": "https://mms.pinduoduo.com/api/config", "path": "/api/config", "promo": False, "body": config},
                {"kind": "json", "url": "https://yingxiao.pinduoduo.com/api/user/info", "path": "/api/user/info", "promo": True, "body": auth},
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "path": "/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "promo": True,
                    "body": {"result": {"mallId": 753391861, "mallName": "御品渔获专营店", "yesData": {"payOrdrAmt": "5538.6", "payOrdrCnt": "122", "gpv": "2722"}}},
                },
            ],
            today=date(2026, 10, 26),
        )
        self.assertEqual([item.name for item in payload.goods], ["全店商品数据"])
        self.assertEqual(payload.images, [])
        self.assertIn("测图测款未写入", payload.note)
        self.assertIn("商品数据", payload.note)
        self.assertNotIn("没有从报表里识别到", payload.note)
        message = describe_sync(payload, {"campaigns": 1, "goods": 1, "images": 0, "days": 1})
        self.assertIn("登录态为空", message)
        self.assertIn("测图测款未写入", message)
        self.assertIn("不是推广测款", message)
        self.assertNotIn("测款 0", message)
        self.assertNotIn("已同步", message)

    def test_creative_and_goods_rows_count_as_promo_not_sydney(self):
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "url": "https://yingxiao.pinduoduo.com/goods/promotion/list",
                    "path": "/goods/promotion/list",
                    "promo": True,
                    "body": {
                        "mallId": 753391861,
                        "mallName": "御品渔获专营店",
                        "result": {
                            "list": [
                                {
                                    "statDate": "2026-10-25",
                                    "goodsId": 9001,
                                    "goodsName": "渔获单品",
                                    "scene": "商品推广",
                                    "impression": 100,
                                    "click": 8,
                                    "spend": 12.5,
                                    "gmv": 40,
                                    "orderNum": 2,
                                }
                            ]
                        },
                    },
                },
                {
                    "kind": "json",
                    "url": "https://yingxiao.pinduoduo.com/goods/report/promotion/overView",
                    "path": "/goods/report/promotion/overView",
                    "promo": True,
                    "body": {
                        "result": {
                            "list": [
                                {
                                    "statDate": "2026-10-25",
                                    "creativeId": "c1",
                                    "creativeName": "主图A",
                                    "goodsId": 9001,
                                    "goodsName": "渔获单品",
                                    "impression": 80,
                                    "click": 6,
                                    "spend": 4.0,
                                    "gmv": 20,
                                    "orderNum": 1,
                                }
                            ]
                        }
                    },
                },
            ],
            today=date(2026, 10, 26),
        )
        self.assertEqual([item.name for item in payload.goods], ["渔获单品"])
        self.assertEqual([item.label for item in payload.images], ["主图A"])
        message = describe_sync(payload, {"campaigns": 1, "goods": 1, "images": 1, "days": 1})
        self.assertIn("测款 1", message)
        self.assertIn("测图 1", message)
        self.assertIn("已同步", message)
        self.assertNotIn("登录态为空", message)

    def test_capture_keeps_url_path_promo_and_drops_giant_config(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "monitor.sqlite"
        giant = {"help": "花费" * 20000, "mallId": 753391861, "mallName": "御品渔获专营店"}
        auth = {"errorMsg": "登录态为空", "mikasaErrorCode": 10, "result": None}
        promo = {
            "mallId": 753391861,
            "result": {"list": [{"goodsId": 1, "goodsName": "渔获单品", "statDate": "2026-10-25", "spend": 3.5, "impression": 10}]},
        }
        seen = [
            {"url": "https://mms.pinduoduo.com/api/config", "path": "/api/config", "promo": False, "status": 200},
            {"url": "https://yingxiao.pinduoduo.com/api/user/info", "path": "/api/user/info", "promo": True, "status": 200},
            {"url": "https://yingxiao.pinduoduo.com/goods/report/promotion/overView", "path": "/goods/report/promotion/overView", "promo": True, "status": 200},
        ]
        _save_capture(
            db_path,
            [
                {"kind": "json", "url": seen[0]["url"], "path": seen[0]["path"], "promo": False, "body": giant},
                {"kind": "json", "url": seen[1]["url"], "path": seen[1]["path"], "promo": True, "body": auth},
                {"kind": "json", "url": "https://yingxiao.pinduoduo.com/goods/report/promotion/overView?access_token=should-not-store", "path": seen[2]["path"], "promo": True, "body": promo},
            ],
            seen,
        )
        raw = capture_path(db_path).read_text(encoding="utf-8")
        self.assertNotIn("should-not-store", raw)
        self.assertLess(len(raw), 200_000)
        data = json.loads(raw)
        self.assertEqual(data[0]["kind"], "debug")
        self.assertEqual(data[0]["urls"][2]["path"], "/goods/report/promotion/overView")
        self.assertTrue(data[0]["urls"][2]["promo"])
        auth_item = next(item for item in data if item.get("path") == "/api/user/info")
        self.assertEqual(auth_item["url"], "https://yingxiao.pinduoduo.com/api/user/info")
        self.assertTrue(auth_item["promo"])
        self.assertIn("登录态为空", json.dumps(auth_item["body"], ensure_ascii=False))
        promo_item = next(item for item in data if item.get("path") == "/goods/report/promotion/overView")
        self.assertTrue(promo_item["promo"])
        self.assertIn("url", promo_item)
        stored_help = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("花费" * 100, stored_help)

    def test_leaving_sso_requires_yingxiao_page_or_cookie(self):
        home = "https://mms.pinduoduo.com/home/"
        sso = "https://mms.pinduoduo.com/login/sso?platform=yingxiao"
        report = "https://yingxiao.pinduoduo.com/goods/report/promotion/overView"
        self.assertFalse(promo_session_ready([home], [{"name": "PASS_ID", "domain": ".pinduoduo.com"}]))
        self.assertFalse(promo_session_ready([sso], [{"name": "PASS_ID", "domain": "mms.pinduoduo.com"}]))
        self.assertFalse(promo_session_ready([sso], [".yingxiao.pinduoduo.com"]))
        self.assertFalse(
            promo_session_ready([sso], [{"name": "_nano_fp", "domain": ".yingxiao.pinduoduo.com"}])
        )
        self.assertFalse(
            promo_session_ready([sso], [{"name": "api_uid", "domain": ".yingxiao.pinduoduo.com"}])
        )
        self.assertTrue(
            promo_session_ready([sso], [{"name": "SUB_PASS_ID", "domain": ".yingxiao.pinduoduo.com"}])
        )
        self.assertTrue(promo_session_ready([report], []))
        self.assertTrue(promo_session_ready(["http://127.0.0.1:9/goods/report/promotion/overView"], []))
        self.assertFalse(promo_session_ready(["http://127.0.0.1:9/home/"], []))
        target = redirect_target(promo_sso_url("https://yingxiao.pinduoduo.com/goods/promotion/list"))
        self.assertEqual(target, "https://yingxiao.pinduoduo.com/goods/promotion/list")

    def test_avatar_token_invalid_records_home_spend_without_creative_rows(self):
        today = date(2026, 10, 26)
        token = {"success": False, "errorCode": 2, "errorMsg": "Token不合法"}
        self.assertTrue(body_token_invalid(token))
        self.assertFalse(json_has_metrics(token))
        self.assertEqual(
            parse_home_promo_spend_text("推广花费 今日 220.15 昨日 386.40"),
            ("220.15", "386.40"),
        )
        self.assertEqual(
            parse_home_promo_spend_text("今日 推广花费 1,220.15 昨日 推广花费 386.40"),
            ("1220.15", "386.40"),
        )
        self.assertEqual(parse_home_promo_spend_text("推广花费 今日 12.5% 昨日 386"), (None, "386"))
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/janus/api/subSystem/generateAccessToken",
                    "path": "/janus/api/subSystem/generateAccessToken",
                    "promo": False,
                    "body": token,
                },
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/home/",
                    "path": "/home/",
                    "promo": False,
                    "body": {
                        "source": "mms-home-ui",
                        "mallName": "港珠汇富专卖店",
                        "todayPromoSpend": "220.15",
                        "yesPromoSpend": "386.40",
                    },
                },
                {
                    "kind": "json",
                    "url": "https://mms.pinduoduo.com/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "path": "/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
                    "body": {
                        "result": {
                            "mallId": 753391861,
                            "mallName": "港珠汇富专卖店",
                            "todayData": {"adSpendAmt": "220.15"},
                            "yesData": {"payOrdrAmt": "5538.6", "payOrdrCnt": "122", "gpv": "10"},
                        }
                    },
                },
            ],
            shop_hint="港珠汇富专卖店",
            today=today,
        )
        names = [item.name for item in payload.goods]
        self.assertIn("首页推广花费", names)
        self.assertIn("全店商品数据", names)
        self.assertEqual(payload.images, [])
        home = next(item for item in payload.goods if item.name == "首页推广花费")
        spend = {day.stat_date: day.spend_fen for day in payload.goods_days[home.goods_id]}
        self.assertEqual(spend["2026-10-26"], 22015)
        self.assertEqual(spend["2026-10-25"], 38640)
        sydney = next(item for item in payload.goods if item.name == "全店商品数据")
        self.assertEqual(payload.goods_days[sydney.goods_id][0].spend_fen, 0)
        self.assertIn("Token不合法", payload.note)
        self.assertIn("首页", payload.note)
        self.assertNotIn("这次没有写入推广花费", payload.note)
        message = describe_sync(payload, {"campaigns": 1, "goods": 2, "images": 0, "days": 2})
        self.assertNotIn("已同步", message)
        self.assertNotIn("测款 1", message)
        self.assertNotIn("测图 1", message)
        self.assertIn("测图测款未写入", message)

    def test_creative_report_wins_over_avatar_token_error(self):
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "body": {"success": False, "errorCode": 2, "errorMsg": "Token不合法"},
                },
                {
                    "kind": "json",
                    "body": {"source": "mms-home-ui", "todayPromoSpend": "220.15", "yesPromoSpend": "386.40"},
                },
                {
                    "kind": "json",
                    "url": "https://yingxiao.pinduoduo.com/goods/report/promotion/overView",
                    "path": "/goods/report/promotion/overView",
                    "promo": True,
                    "body": {
                        "mallId": 753391861,
                        "mallName": "港珠汇富专卖店",
                        "result": {
                            "list": [
                                {
                                    "statDate": "2026-10-25",
                                    "goodsId": 9001,
                                    "goodsName": "渔获单品",
                                    "impression": 80,
                                    "click": 6,
                                    "spend": 4.0,
                                    "gmv": 20,
                                    "orderNum": 1,
                                },
                                {
                                    "statDate": "2026-10-25",
                                    "creativeId": "c1",
                                    "creativeName": "主图A",
                                    "goodsId": 9001,
                                    "goodsName": "渔获单品",
                                    "impression": 80,
                                    "click": 6,
                                    "spend": 4.0,
                                    "gmv": 20,
                                    "orderNum": 1,
                                },
                            ]
                        },
                    },
                },
            ],
            today=date(2026, 10, 26),
        )
        self.assertEqual([item.name for item in payload.goods], ["渔获单品"])
        self.assertEqual([item.label for item in payload.images], ["主图A"])
        message = describe_sync(payload, {"campaigns": 1, "goods": 1, "images": 1, "days": 1})
        self.assertIn("已同步", message)
        self.assertIn("测图 1", message)
        self.assertNotIn("Token不合法", message)
        self.assertNotIn("首页推广花费", message)
