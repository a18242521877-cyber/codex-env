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
DEFAULT_REPORT_URLS = (
    OFFICIAL_PROMO_URL,
    "https://yingxiao.pinduoduo.com/",
    "https://mms.pinduoduo.com/home/",
    "https://yingxiao.pinduoduo.com/mains/promotion/report",
    "https://yingxiao.pinduoduo.com/mains/goods/promotion",
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
                        captures = self._harvest(page, context, db_path, keep_session=job is not None)
                    except AppError:
                        if job is not None:
                            from app.services.live_login import retain_saved_login

                            retain_saved_login(db_path, job)
                            return None
                        raise
                    hint = _shop_name(page) or hint
                    from app.services.live_login import generation_is_current, retain_saved_login, update_live_payload

                    if job is not None and not generation_is_current(getattr(job, "generation", None)):
                        retain_saved_login(db_path, job)
                        return None
                    _save_capture(db_path, captures)
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

    def _harvest(self, page, context, db_path: Path, keep_session: bool = False) -> list[dict]:
        bucket: list[dict] = []

        def on_response(response) -> None:
            _take_response(response, bucket)

        page.on("response", on_response)
        for url in self.report_urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            if "/login" in (page.url or "") and not _has_login_cookie(context):
                if keep_session:
                    return bucket
                from app.services.live_login import drop_live_login

                drop_live_login(db_path)
                raise AppError("登录已失效，请重新扫码", status=401, code="login_required")
            if "/login" in (page.url or ""):
                continue
            page.wait_for_timeout(1200)
            _click_exports(page, bucket)
        return bucket


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


def _save_capture(db_path: Path, captures: list[dict]) -> None:
    preview = []
    for item in captures[:40]:
        kind = item.get("kind")
        body = item.get("body")
        if kind == "json":
            preview.append({"kind": "json", "body": body})
        else:
            size = len(body) if isinstance(body, (bytes, bytearray)) else None
            preview.append({"kind": kind, "bytes": size})
    raw = json.dumps(preview, ensure_ascii=False, default=str)
    if len(raw) > 200_000:
        raw = raw[:200_000]
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


def _take_response(response, bucket: list[dict]) -> None:
    try:
        if response.status >= 400:
            return
        ctype = (response.headers or {}).get("content-type", "")
        url = response.url.split("?", 1)[0]
        if "json" in ctype or url.endswith(".json"):
            bucket.append({"kind": "json", "body": response.json()})
            return
        if "csv" in ctype or url.endswith(".csv"):
            body = response.body()
            if len(body) <= 5_000_000:
                bucket.append({"kind": "csv", "body": body})
            return
        if "spreadsheet" in ctype or "excel" in ctype or url.endswith(".xlsx"):
            body = response.body()
            if len(body) <= 5_000_000:
                bucket.append({"kind": "xlsx", "body": body})
    except Exception:
        return


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
