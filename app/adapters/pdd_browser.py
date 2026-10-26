"""用本机浏览器打开拼多多商家后台。

登录是店主自己扫码。之后只保存 Playwright 的 storage state，不把 Cookie 放进
数据库，也不发给别的机器。同步时打开报表页，收下页面自己发出的 JSON / CSV / xlsx。
不伪造接口签名，不绕过验证码，也不点击「生成报表」（那会在账号里生成新报表）。

页面一改，二维码选择器或字段名就可能对不上。对不上时保留会话并说明没识别到数据，
不编造花费。
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

from app.adapters.pdd_normalize import normalize_captures
from app.adapters.pdd_session import (
    PASS_COOKIE_NAMES,
    capture_path,
    is_shop_token_cookie,
    page_looks_logged_in,
    storage_state_path,
    tighten,
    write_private_text,
)
from app.config import OFFICIAL_LOGIN_URL, OFFICIAL_PROMO_URL
from app.errors import AppError

LOGIN_COOKIE_NAMES = PASS_COOKIE_NAMES
MMS_HOME_URL = "https://mms.pinduoduo.com/home/"
YINGXIAO_ORIGIN = "https://yingxiao.pinduoduo.com"
# 这些是推广中心页面，不是我们自己拼的数据接口。营销域名需要先走商家后台的登录跳转。
YINGXIAO_PAGES = (
    "/mains/account/report",
    "/mains/goods/promotion",
    "/goods/promotion/list",
    "/",
)
PROMO_URL_HINTS = ("report", "promotion", "/ad", "advertise", "creative", "campaign", "goods", "plan")
NAV_LABELS = ("推广中心", "推广平台", "多多招财", "商品推广", "推广报表", "推广概况", "账户报表")
DATE_LABELS = ("近7天", "近7日", "最近7天", "昨天", "近30天", "近30日")
_MAX_JSON = 36
_MAX_URLS = 80
def promo_sso_url(target: str) -> str:
    """商家后台跳进营销域名的公开入口。查询参数里只有要打开的页面地址。"""
    return (
        "https://mms.pinduoduo.com/login/sso?accessType=auto&platform=yingxiao&redirectUrl="
        + quote(target, safe="")
    )


def default_report_urls() -> tuple[str, ...]:
    pages = [MMS_HOME_URL, promo_sso_url(OFFICIAL_PROMO_URL)]
    for path in YINGXIAO_PAGES:
        target = YINGXIAO_ORIGIN + "/" if path == "/" else YINGXIAO_ORIGIN + path
        entry = promo_sso_url(target)
        if entry not in pages:
            pages.append(entry)
    return tuple(pages)


DEFAULT_REPORT_URLS = default_report_urls()


def split_response_url(url: str) -> tuple[str, str]:
    """丢掉查询参数。临时令牌经常出现在 ? 后面，不能写进捕获文件。"""
    parts = urlsplit(url or "")
    base = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc else (parts.path or "")
    return base, parts.path or "/"


def is_promo_response_url(url: str) -> bool:
    base, path = split_response_url(url)
    hay = (base + path).lower()
    return any(hint in hay for hint in PROMO_URL_HINTS)


def is_promo_nav_label(label: str) -> bool:
    text = " ".join((label or "").split())
    if not text or len(text) > 12 or any(word in text for word in ("生成", "开启", "新建", "删除", "暂停")):
        return False
    return text in NAV_LABELS


def is_date_preset_label(label: str) -> bool:
    text = " ".join((label or "").split())
    if not text or "生成" in text:
        return False
    return text in DATE_LABELS


def body_looks_like_mikasa(body) -> bool:
    """推广接口未登录时常见：mikasaErrorCode，并且 result 是空的。"""
    found = False

    def walk(node, depth: int) -> None:
        nonlocal found
        if found or depth > 6:
            return
        if isinstance(node, dict):
            if "mikasaErrorCode" in node or "mikasa_error_code" in node:
                result = node.get("result", node.get("Result", "missing"))
                if result in (None, {}, []):
                    found = True
                    return
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)

    walk(body, 0)
    return found


def json_has_mall(body) -> bool:
    wanted = {"mallid", "mall_id", "mallname", "mall_name"}

    def walk(node, depth: int) -> bool:
        if depth > 5:
            return False
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower().replace("-", "_") in wanted and value not in (None, ""):
                    return True
                if isinstance(value, (dict, list)) and walk(value, depth + 1):
                    return True
        elif isinstance(node, list):
            return any(walk(item, depth + 1) for item in node[:30])
        return False

    return walk(body, 0)


def json_has_metrics(body) -> bool:
    try:
        text = json.dumps(body, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return False
    return any(mark in text for mark in ("spend", "goodsid", "creativeid", "ordernum", "impression", "成交花费", "花费", "曝光"))


def bucket_has_metrics(bucket: list[dict]) -> bool:
    for item in bucket:
        kind = item.get("kind")
        if kind in {"csv", "xlsx"}:
            return True
        if kind == "json" and item.get("promo") and json_has_metrics(item.get("body")) and not body_looks_like_mikasa(item.get("body")):
            return True
    return False


def guidance_note(bucket: list[dict], seen: list[dict]) -> str:
    if bucket_has_metrics(bucket):
        return ""
    mikasa = any(item.get("kind") == "json" and body_looks_like_mikasa(item.get("body")) for item in bucket)
    if mikasa:
        return (
            "已登录商家后台，但推广中心返回了空结果（mikasa）。"
            "请在弹出的窗口里点「推广中心」或「商品推广」，看到花费后再点同步。"
            "程序不会点「生成报表」。"
        )
    if not any(item.get("promo") for item in seen):
        return (
            "已登录商家后台，但没有收到推广报表请求。"
            "请在弹出的窗口从首页进入「推广中心」，停留在能看到花费的页面后再同步。"
        )
    return (
        "已打开推广页面，但没有识别到花费、商品或创意。"
        "请在弹出的窗口把日期选到有数据的范围后再同步。程序不会点「生成报表」。"
    )


_QR_SELECTORS = (
    "img.qrcode",
    "img[class*='qrcode']",
    "img[class*='qr-code']",
    "img[src*='qr']",
    "canvas",
    ".qrcode",
)


def is_export_label(label: str) -> bool:
    text = " ".join((label or "").split())
    if not text or len(text) > 16 or "生成" in text:
        return False
    return "下载" in text or "导出" in text


class PlaywrightLiveDriver:
    def __init__(self, *, login_url: str | None = None, report_urls: tuple[str, ...] | list[str] | None = None, headless: bool = False, channel: str | None = None):
        self.login_url = login_url or OFFICIAL_LOGIN_URL
        self.report_urls = list(report_urls or DEFAULT_REPORT_URLS)
        self.headless = headless
        self.channel = channel

    def check_ready(self) -> None:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError as exc:
            raise AppError(
                "还没有安装浏览器组件。请执行：pip install playwright && python -m playwright install chromium。"
                "Windows 上如果已经安装 Edge，装好 playwright 后会优先用 Edge。",
                status=503,
                code="browser_missing",
            ) from exc

    def run(self, job, db_path) -> None:
        from app.config import LIVE_LOGIN_SECONDS

        self._browse(db_path, job=job, today=date.today(), wait_seconds=LIVE_LOGIN_SECONDS, restore=False)

    def collect(self, db_path, *, today: date):
        return self._browse(db_path, job=None, today=today, wait_seconds=0, restore=True)

    def _browse(self, db_path, *, job, today: date, wait_seconds: int, restore: bool):
        from playwright.sync_api import sync_playwright

        db_path = Path(db_path)
        try:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                try:
                    options = {
                        "locale": "zh-CN",
                        "accept_downloads": True,
                        "viewport": {"width": 1280, "height": 860},
                    }
                    if restore:
                        options["storage_state"] = str(storage_state_path(db_path))
                    context = browser.new_context(**options)
                    page = context.new_page()
                    if restore:
                        self._open_first(page)
                        if not _has_login_cookie(context):
                            from app.services.live_login import drop_live_login

                            drop_live_login(db_path)
                            raise AppError("登录已失效，请重新扫码", status=401, code="login_required")
                    else:
                        if not self._wait_for_scan(page, job, db_path, wait_seconds):
                            return None
                        if _cancelled(job) and not job.snapshot().get("cookies_saved"):
                            return None
                        _save_storage(context, db_path)
                        from app.services.live_login import READING_MESSAGE, finish_live_session

                        hint = _shop_name(page)
                        job.update(cookies_saved=True, message=READING_MESSAGE)
                        provisional = normalize_captures([], shop_hint=hint or "拼多多店铺", today=today)
                        if not provisional.note:
                            provisional.note = "已登录。报表还在读取，进入店铺后等十几秒再刷新页面。"
                        result = finish_live_session(
                            db_path,
                            provisional,
                            job.ticket,
                            generation=getattr(job, "generation", None),
                        )
                        if result.get("skipped"):
                            return None
                        job.update(
                            status="confirmed",
                            finished=True,
                            cookies_saved=True,
                            message=result.get("message") or READING_MESSAGE,
                        )
                    hint = _shop_name(page)
                    try:
                        captures, seen = self._harvest(page, context, db_path, keep_session=job is not None)
                    except AppError:
                        if job is not None:
                            from app.services.live_login import retain_saved_login

                            retain_saved_login(db_path, job)
                            return None
                        raise
                    hint = _shop_name(_latest_page(context, page)) or hint
                    from app.services.live_login import generation_is_current, retain_saved_login, update_live_payload

                    if job is not None and not generation_is_current(getattr(job, "generation", None)):
                        retain_saved_login(db_path, job)
                        return None
                    _save_capture(db_path, captures, seen)
                    payload = normalize_captures(captures, shop_hint=hint, today=today)
                    if job is None:
                        return payload

                    updated = update_live_payload(db_path, payload, generation=getattr(job, "generation", None))
                    if updated and updated.get("message"):
                        job.update(message=updated["message"])
                    retain_saved_login(db_path, job)
                    return None
                finally:
                    browser.close()
        except AppError:
            raise
        except Exception as exc:
            raise _browser_error(exc) from exc

    def _launch(self, playwright):
        if self.channel:
            return playwright.chromium.launch(channel=self.channel, headless=self.headless)
        if not self.headless:
            try:
                return playwright.chromium.launch(channel="msedge", headless=False)
            except Exception:
                pass
        return playwright.chromium.launch(headless=self.headless)

    def _wait_for_scan(self, page, job, db_path: Path, wait_seconds: int) -> bool:
        import time

        try:
            page.goto(self.login_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            raise _browser_error(exc) from exc
        def _on_frame(frame) -> None:
            del frame

        try:
            page.on("framenavigated", _on_frame)
        except Exception:
            pass
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if _cancelled(job) and not job.snapshot().get("cookies_saved"):
                return False
            image = _qr_image(page)
            if image:
                job.update(qr_image=image, message="请用拼多多商家版 App 扫描弹出窗口里的二维码")
            if _logged_in(page.context, page):
                from app.services.live_login import READING_MESSAGE

                job.update(message=READING_MESSAGE)
                return True
            try:
                page.wait_for_url(lambda url: "/login" not in (url or ""), timeout=400)
            except Exception:
                page.wait_for_timeout(200)
        from app.services.live_login import mark_ticket

        mark_ticket(db_path, job.ticket, "expired")
        job.update(status="expired", message="扫码等待超时，请重新打开登录")
        return False

    def _open_first(self, page) -> None:
        target = self.report_urls[0] if self.report_urls else self.login_url
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            raise _browser_error(exc) from exc

    def _harvest(self, page, context, db_path: Path, keep_session: bool = False):
        bucket: list[dict] = []
        seen: list[dict] = []

        def on_response(response) -> None:
            _take_response(response, bucket, seen)

        context.on("response", on_response)
        for url in self.report_urls:
            if bucket_has_metrics(bucket):
                break
            active = _latest_page(context, page)
            try:
                active.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            _settle(active)
            active = _latest_page(context, active)
            if _stuck_on_login(active.url or "") and not _has_login_cookie(context):
                if keep_session:
                    return bucket, seen
                from app.services.live_login import drop_live_login

                drop_live_login(db_path)
                raise AppError("登录已失效，请重新扫码", status=401, code="login_required")
            if not bucket_has_metrics(bucket):
                _click_exact(active, NAV_LABELS)
                active = _latest_page(context, active)
                _settle(active)
            if not bucket_has_metrics(bucket):
                _click_exact(active, DATE_LABELS)
                active = _latest_page(context, active)
                _settle(active)
            _click_exports(active, bucket)
        note = guidance_note(bucket, seen)
        if note:
            bucket.append({"kind": "note", "body": note})
        return bucket, seen


def _browser_error(exc: Exception) -> AppError:
    text = str(exc).lower()
    if "pass_id" in text or "cookie" in text or "set-cookie" in text:
        return AppError("浏览器操作失败，请重试。", status=502, code="browser_failed")
    if "executable" in text or "please run the following" in text:
        return AppError(
            "找不到浏览器。请执行 python -m playwright install chromium，或安装 Microsoft Edge。",
            status=503,
            code="browser_missing",
        )
    if "timeout" in text:
        return AppError("打开页面超时。请检查网络后重试。", status=504, code="browser_timeout")
    return AppError("浏览器操作失败，请重试。", status=502, code="browser_failed")


def _cancelled(job) -> bool:
    if job is None:
        return False
    return bool(job.snapshot().get("cancelled"))


def _cookie_names(context) -> set[str]:
    try:
        return {str(item.get("name") or "") for item in context.cookies()}
    except Exception:
        return set()


def _has_login_cookie(context) -> bool:
    names = _cookie_names(context)
    if names & LOGIN_COOKIE_NAMES:
        return True
    return any(is_shop_token_cookie(name) for name in names)


def _logged_in(context, page) -> bool:
    return page_looks_logged_in(_cookie_names(context), page.url or "")


def _save_storage(context, db_path: Path) -> None:
    path = storage_state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    tighten(path)


def _save_capture(db_path: Path, captures: list[dict], seen: list[dict] | None = None) -> None:
    debug = {"kind": "debug", "urls": list(seen or [])[:_MAX_URLS]}
    preview = [debug]
    for item in captures:
        kind = item.get("kind")
        if kind == "json":
            preview.append(
                {
                    "kind": "json",
                    "url": item.get("url") or "",
                    "path": item.get("path") or "",
                    "body": item.get("body"),
                }
            )
        elif kind == "note":
            preview.append({"kind": "note", "body": item.get("body") or ""})
        else:
            body = item.get("body")
            size = len(body) if isinstance(body, (bytes, bytearray)) else None
            preview.append({"kind": kind, "url": item.get("url") or "", "bytes": size})
    raw = json.dumps(preview, ensure_ascii=False, default=str)
    while len(raw) > 200_000 and len(preview) > 1:
        preview.pop()
        raw = json.dumps(preview, ensure_ascii=False, default=str)
    write_private_text(capture_path(db_path), raw)


def _qr_image(page) -> str | None:
    for selector in _QR_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            png = loc.screenshot(type="png", timeout=1000)
        except Exception:
            continue
        if png:
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return None


def _shop_name(page) -> str:
    for selector in (".shop-name", "[class*='shopName']", "[class*='mallName']", "[class*='ShopName']"):
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            text = loc.inner_text(timeout=800).strip()
        except Exception:
            continue
        if text:
            return text[:40]
    return ""


def _stuck_on_login(url: str) -> bool:
    path = urlsplit(url or "").path or ""
    # /login/sso 是跳进推广中心的中转，不是扫码页。
    if path.rstrip("/").endswith("/login/sso"):
        return False
    return "/login" in path


def _latest_page(context, fallback):
    try:
        pages = [item for item in context.pages if not item.is_closed()]
    except Exception:
        return fallback
    return pages[-1] if pages else fallback


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass


def _click_exact(page, labels: tuple[str, ...]) -> bool:
    for label in labels:
        if not (is_promo_nav_label(label) or is_date_preset_label(label)):
            continue
        try:
            loc = page.get_by_text(label, exact=True)
            count = min(loc.count(), 4)
        except Exception:
            continue
        for index in range(count):
            node = loc.nth(index)
            try:
                if not node.is_visible():
                    continue
                node.click(timeout=1200, no_wait_after=True)
            except Exception:
                continue
            try:
                popup = page.wait_for_event("popup", timeout=1200)
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
            except Exception:
                pass
            return True
    return False


def _take_response(response, bucket: list[dict], seen: list[dict] | None = None) -> None:
    try:
        ctype = (response.headers or {}).get("content-type", "")
        base, path = split_response_url(response.url or "")
        promo = is_promo_response_url(base)
        is_json = "json" in ctype or path.endswith(".json")
        is_csv = "csv" in ctype or path.endswith(".csv")
        is_xlsx = "spreadsheet" in ctype or "excel" in ctype or path.endswith(".xlsx")
        if not (is_json or is_csv or is_xlsx):
            return
        if seen is not None:
            seen.append({"url": base, "path": path, "promo": promo, "status": response.status})
            if len(seen) > _MAX_URLS:
                del seen[:-_MAX_URLS]
        if response.status >= 400:
            return
        if is_json:
            body = response.json()
            item = {"kind": "json", "url": base, "path": path, "body": body, "promo": promo}
            if promo:
                _remember_json(bucket, item)
            elif json_has_mall(body) and not any(entry.get("role") == "identity" for entry in bucket):
                item["role"] = "identity"
                bucket.append(item)
            return
        body = response.body()
        if len(body) > 5_000_000:
            return
        if is_csv:
            bucket.append({"kind": "csv", "url": base, "path": path, "body": body, "promo": True})
        elif is_xlsx:
            bucket.append({"kind": "xlsx", "url": base, "path": path, "body": body, "promo": True})
    except Exception:
        return


def _remember_json(bucket: list[dict], item: dict) -> None:
    bucket.append(item)
    json_items = [entry for entry in bucket if entry.get("kind") == "json" and entry.get("role") != "identity"]
    if len(json_items) <= _MAX_JSON:
        return
    for old in json_items:
        if "yingxiao.pinduoduo.com" not in (old.get("url") or ""):
            bucket.remove(old)
            return
    bucket.remove(json_items[0])


def _click_exports(page, bucket: list[dict]) -> None:
    try:
        handles = page.locator("a, button")
        count = min(handles.count(), 40)
    except Exception:
        return
    clicked = 0
    for index in range(count):
        if clicked >= 3:
            return
        node = handles.nth(index)
        try:
            label = node.inner_text(timeout=400)
        except Exception:
            continue
        if not is_export_label(label):
            continue
        try:
            with page.expect_download(timeout=4000) as downloaded:
                node.click(timeout=1500)
            target = downloaded.value.path()
            if not target:
                continue
            data = Path(target).read_bytes()
            name = (downloaded.value.suggested_filename or "").lower()
            kind = "xlsx" if name.endswith((".xlsx", ".xls")) else "csv"
            if len(data) <= 5_000_000:
                bucket.append({"kind": kind, "body": data})
            clicked += 1
        except Exception:
            continue
