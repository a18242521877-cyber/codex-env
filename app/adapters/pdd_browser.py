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
import queue
import threading
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from app.adapters.pdd_normalize import (
    AUTH_FAILED_NOTE,
    MMS_LOGIN_NOTE,
    SAVED_LOGIN_NOTE,
    TOKEN_INVALID_NOTE,
    body_auth_failed,
    body_looks_like_mikasa,
    body_token_invalid,
    json_has_creative_metrics,
    json_has_metrics,
    manual_login_tail,
    normalize_captures,
    parse_home_promo_spend_text,
)
from app.adapters.pdd_session import (
    PASS_COOKIE_NAMES,
    capture_path,
    is_shop_token_cookie,
    page_looks_logged_in,
    storage_state_path,
    tighten,
    write_private_text,
)
from app.config import OFFICIAL_LOGIN_URL
from app.errors import AppError

LOGIN_COOKIE_NAMES = PASS_COOKIE_NAMES
MMS_HOME_URL = "https://mms.pinduoduo.com/home/"
# 商家后台侧栏里的推广入口。直接打开营销域名会停在 /login/sso，要先等它跳走。
SIDEBAR_TARGETS = (
    "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "https://yingxiao.pinduoduo.com/goods/promotion/list",
    "https://yingxiao.pinduoduo.com/goods/report/promotion/overView",
    "https://yingxiao.pinduoduo.com/mains/account/finance",
)
GOODS_EFFECT_URL = "https://mms.pinduoduo.com/sycm/goods_effect/detail"
PROMO_URL_HINTS = ("report", "promotion", "/ad", "advertise", "creative", "campaign", "plan", "goodsdatashow", "sydney")
NAV_LABELS = ("推广概况", "推广中心", "推广平台", "多多招财", "商品推广", "推广报表", "推广账户", "账户报表")
# 同步不代点登录入口。这些是已经进到报表后的页签。
REPORT_NAV = ("推广概况", "商品推广", "推广报表", "推广账户", "账户报表")
DIMENSION_LABELS = ("商品", "创意", "主图", "推广商品")
SSO_STUCK_NOTE = AUTH_FAILED_NOTE
DATE_LABELS = ("近7天", "近7日", "最近7天", "昨天", "近30天", "近30日")
_MAX_JSON = 36
_MAX_URLS = 80
_MAX_JSON_CHARS = 12_000
def promo_sso_url(target: str) -> str:
    """商家后台跳进营销域名的公开入口。查询参数里只有要打开的页面地址。"""
    return (
        "https://mms.pinduoduo.com/login/sso?accessType=auto&platform=yingxiao&redirectUrl="
        + quote(target, safe="")
    )


def default_report_urls() -> tuple[str, ...]:
    """同步直接打开固定页面。不走 accessType=auto，也不代点头像。"""
    return (MMS_HOME_URL, GOODS_EFFECT_URL, *SIDEBAR_TARGETS)


DEFAULT_REPORT_URLS = default_report_urls()


def split_response_url(url: str) -> tuple[str, str]:
    """丢掉查询参数。临时令牌经常出现在 ? 后面，不能写进捕获文件。"""
    parts = urlsplit(url or "")
    base = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc else (parts.path or "")
    return base, parts.path or "/"


def is_promo_response_url(url: str) -> bool:
    base, path = split_response_url(url)
    host = urlsplit(base).netloc.lower()
    if host == "yingxiao.pinduoduo.com" or host.endswith(".yingxiao.pinduoduo.com"):
        return True
    hay = (host + path).lower()
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


def is_dimension_label(label: str) -> bool:
    text = " ".join((label or "").split())
    if not text or len(text) > 8 or any(word in text for word in ("生成", "开启", "新建", "删除", "暂停")):
        return False
    return text in DIMENSION_LABELS


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


def bucket_has_metrics(bucket: list[dict]) -> bool:
    for item in bucket:
        kind = item.get("kind")
        if kind in {"csv", "xlsx"} and item.get("promo", True):
            return True
        if kind == "json" and json_has_metrics(item.get("body")) and not body_auth_failed(item.get("body")):
            return True
    return False


def bucket_has_creative(bucket: list[dict]) -> bool:
    return any(item.get("kind") == "json" and json_has_creative_metrics(item.get("body")) for item in bucket)


def bucket_auth_failed(bucket: list[dict]) -> bool:
    return any(item.get("kind") == "json" and body_auth_failed(item.get("body")) for item in bucket)


def bucket_token_invalid(bucket: list[dict]) -> bool:
    return any(item.get("kind") == "json" and body_token_invalid(item.get("body")) for item in bucket)


def _auth_blocks_promo(bucket: list[dict]) -> bool:
    """头像换票失败且没有报表时，营销域名和 _nano_fp 都不能当成授权成功。"""
    return bucket_token_invalid(bucket) and not bucket_has_metrics(bucket)


def on_sso_page(url: str) -> bool:
    path = urlsplit(url or "").path or ""
    return path.rstrip("/").endswith("/login/sso")


def is_sso_entry(url: str) -> bool:
    parts = urlsplit(url or "")
    if on_sso_page(url):
        return True
    return "platform=yingxiao" in (parts.query or "")


def capture_has_sydney(bucket: list[dict]) -> bool:
    for item in bucket:
        if item.get("kind") != "json":
            continue
        url = (item.get("url") or "").lower()
        if "goodsdatashow" in url or "/sydney/" in url:
            return True
        body = item.get("body")
        if isinstance(body, dict) and _tree_has_key(body, {"yesdata", "todaydata", "yeslist", "todaylist"}):
            return True
    return False


def _tree_has_key(node, wanted: set[str], depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in wanted:
                return True
            if isinstance(value, (dict, list)) and _tree_has_key(value, wanted, depth + 1):
                return True
    elif isinstance(node, list):
        return any(_tree_has_key(item, wanted, depth + 1) for item in node[:20])
    return False


def yingxiao_host(url: str) -> bool:
    host = urlsplit(url or "").netloc.lower()
    return host == "yingxiao.pinduoduo.com" or host.endswith(".yingxiao.pinduoduo.com")


def promo_surface_url(url: str) -> bool:
    """已经离开授权页、进入推广报表或营销域名。回到商家首页不算。"""
    if not url or on_sso_page(url):
        return False
    if yingxiao_host(url):
        return True
    path = urlsplit(url).path or ""
    return any(marker in path for marker in ("/goods/report/", "/goods/promotion/", "/mains/promotion", "/mains/account/"))


def should_open_report_url(url: str, cookies: list[dict] | None) -> bool:
    """没有推广中心登录时，不要自己打开营销域名，否则会停在 SSO。"""
    if yingxiao_host(url) and not yingxiao_session_ready(cookies):
        return False
    return True


def cookie_breaks_yingxiao_sso(item: dict) -> bool:
    """商家后台的 PASS_ID 会让推广中心 SSO 自动换票。失败后页面反复刷新，二维码也停不住。"""
    if not isinstance(item, dict):
        return False
    domain = str(item.get("domain") or "").lower()
    if "yingxiao" in domain:
        return False
    name = str(item.get("name") or "")
    folded = name.upper()
    if name in {"PASS_ID", "SUB_PASS_ID"} or "PASS" in folded or is_shop_token_cookie(name):
        return True
    return False


def cookies_for_manual_open(url: str, cookies: list[dict] | None) -> list[dict]:
    """手动打开登录页时用的 Cookie。没有推广中心会话就什么都不带。"""
    items = [item for item in (cookies or []) if isinstance(item, dict)]
    if not yingxiao_host(url):
        return []
    kept = [item for item in items if not cookie_breaks_yingxiao_sso(item)]
    if not yingxiao_session_ready(kept):
        return []
    return kept


def should_persist_manual_session(url: str, cookies: list[dict] | None) -> bool:
    """只有已经离开授权页、进入推广中心页面，才把登录写入磁盘。"""
    if not url or on_sso_page(url) or _auth_gate_label(url):
        return False
    if "/login" in (urlsplit(url).path or ""):
        return False
    if not yingxiao_host(url) or not promo_surface_url(url):
        return False
    return yingxiao_session_ready(cookies)


def yingxiao_session_ready(cookies: list[dict] | None) -> bool:
    """营销域名上的 _nano_fp 只是指纹，不是推广中心登录态。"""
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lower()
        name = str(item.get("name") or "")
        if "yingxiao" not in domain:
            continue
        if name in {"_nano_fp", "api_uid"} or name.startswith("_"):
            continue
        folded = name.upper()
        if name in {"PASS_ID", "SUB_PASS_ID"} or "PASS" in folded or "TOKEN" in folded:
            return True
    return False


def promo_session_ready(page_urls: list[str], cookies: list[dict] | None = None) -> bool:
    if any(promo_surface_url(url) for url in page_urls):
        return True
    return yingxiao_session_ready(cookies)


def redirect_target(url: str) -> str:
    values = parse_qs(urlsplit(url or "").query).get("redirectUrl") or []
    if not values:
        return ""
    target = values[0]
    if target.startswith("https://yingxiao.pinduoduo.com/"):
        return target
    host = urlsplit(target).hostname or ""
    if host in {"127.0.0.1", "localhost"}:
        return target
    return ""


def guidance_note(bucket: list[dict], seen: list[dict]) -> str:
    if bucket_has_metrics(bucket):
        return ""
    if bucket_token_invalid(bucket):
        return TOKEN_INVALID_NOTE
    if bucket_auth_failed(bucket):
        return AUTH_FAILED_NOTE
    if capture_has_sydney(bucket):
        return ""
    if not any(item.get("promo") for item in seen):
        return "已登录商家后台，但没有收到推广报表请求。" + manual_login_tail("推广中心页面")
    return (
        "已打开推广页面，但没有识别到花费、商品或创意。"
        + manual_login_tail("推广报表页面")
        + "程序不会点「生成报表」。"
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
    def __init__(
        self,
        *,
        login_url: str | None = None,
        report_urls: tuple[str, ...] | list[str] | None = None,
        headless: bool = False,
        channel: str | None = None,
        sso_probe_seconds: float = 15,
        sso_wait_seconds: float = 180,
    ):
        self.login_url = login_url or OFFICIAL_LOGIN_URL
        self.report_urls = list(report_urls or DEFAULT_REPORT_URLS)
        self.headless = headless
        self.channel = channel
        self.sso_probe_seconds = sso_probe_seconds
        self.sso_wait_seconds = sso_wait_seconds
        self._hold_commands: queue.Queue | None = None
        self._hold_thread: threading.Thread | None = None
        self._hold_guard = threading.Lock()

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

    def run(self, job, db_path, *, open_url: str | None = None, establish_session: bool = True) -> None:
        from app.config import LIVE_LOGIN_SECONDS

        self._browse(
            db_path,
            job=job,
            today=date.today(),
            wait_seconds=LIVE_LOGIN_SECONDS,
            restore=False,
            harvest=False,
            open_url=open_url or self.login_url,
            establish_session=establish_session,
        )

    def collect(self, db_path, *, today: date):
        return self._browse(db_path, job=None, today=today, wait_seconds=0, restore=True, harvest=True)

    def has_open_session(self) -> bool:
        thread = self._hold_thread
        return thread is not None and thread.is_alive()

    def open_url_only(self, db_path, url: str) -> str:
        """弹出 Edge 并打开固定地址。不等扫码，不代点头像，窗口留着给店主登录。"""
        self.check_ready()
        self._ensure_hold(Path(db_path))
        opened = self._hold_call(("goto", str(url)), timeout=45)
        return str(opened or url)

    def harvest_open(self, db_path, today: date):
        """在已经打开的 Edge 窗口里收报表。窗口关了就返回 None，改读已保存的登录。"""
        if not self.has_open_session():
            return None
        try:
            payload = self._hold_call(("harvest", str(db_path), today.isoformat()), timeout=180)
        except AppError as exc:
            if exc.code != "browser_closed":
                raise
            payload = None
        if payload is None:
            thread = self._hold_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=8)
        return payload

    def close_open_session(self) -> None:
        if not self.has_open_session():
            return
        try:
            self._hold_call(("close",), timeout=20)
        except Exception:
            pass
        thread = self._hold_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _ensure_hold(self, db_path: Path) -> None:
        with self._hold_guard:
            if self.has_open_session():
                return
            commands: queue.Queue = queue.Queue()
            ready = threading.Event()
            holder: dict = {}
            thread = threading.Thread(
                target=self._hold_loop,
                args=(db_path, commands, ready, holder),
                name="pdd-edge-hold",
                daemon=True,
            )
            self._hold_commands = commands
            self._hold_thread = thread
            thread.start()
        if not ready.wait(timeout=40):
            raise AppError("浏览器没有打开", status=504, code="browser_timeout")
        error = holder.get("error")
        if isinstance(error, AppError):
            raise error
        if error:
            raise _browser_error(error if isinstance(error, Exception) else Exception(str(error)))

    def _hold_call(self, command: tuple, timeout: float):
        commands = self._hold_commands
        if commands is None or not self.has_open_session():
            raise AppError("浏览器已关闭", status=409, code="browser_closed")
        box: queue.Queue = queue.Queue(maxsize=1)
        commands.put((command, box))
        try:
            ok, value = box.get(timeout=timeout)
        except queue.Empty:
            raise AppError("浏览器没有响应", status=504, code="browser_timeout")
        if not ok:
            if isinstance(value, AppError):
                raise value
            if isinstance(value, Exception):
                raise _browser_error(value)
            raise AppError("浏览器操作失败，请重试。", status=502, code="browser_failed")
        return value

    def _hold_loop(self, db_path: Path, commands: queue.Queue, ready: threading.Event, holder: dict) -> None:
        from playwright.sync_api import sync_playwright

        from app.services.live_login import acquire_browser, release_browser

        if not acquire_browser(8):
            holder["error"] = AppError("浏览器正忙，请稍后再试", status=409, code="browser_busy")
            ready.set()
            return
        browser = None
        try:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                options = {
                    "locale": "zh-CN",
                    "accept_downloads": True,
                    "viewport": {"width": 1280, "height": 860},
                }
                # 不把已保存的商家后台 PASS_ID 灌进这个窗口。推广中心 SSO 会拿它换票，
                # Token不合法 之后整页刷新，二维码停不住。
                context = browser.new_context(**options)
                page = context.new_page()
                ready.set()
                while True:
                    try:
                        command, box = commands.get(timeout=1)
                    except queue.Empty:
                        try:
                            _maybe_save_landed(context, db_path, _latest_page(context, page))
                        except Exception:
                            pass
                        if not _context_has_page(context):
                            break
                        continue
                    kind = command[0]
                    try:
                        if kind == "close":
                            box.put((True, None))
                            break
                        if kind == "goto":
                            page = _latest_page(context, page)
                            if _page_closed(page):
                                page = context.new_page()
                            _prepare_manual_context(context, command[1], db_path)
                            page.goto(command[1], wait_until="domcontentloaded", timeout=20000)
                            _maybe_save_landed(context, db_path, page)
                            box.put((True, _page_url(page)))
                            continue
                        if kind == "harvest":
                            harvest_db = Path(command[1])
                            today = date.fromisoformat(command[2])
                            if not _context_has_page(context):
                                box.put((True, None))
                                break
                            page = _latest_page(context, page)
                            if not yingxiao_session_ready(_cookie_records(context)):
                                box.put((False, AppError(AUTH_FAILED_NOTE, status=409, code="login_required")))
                                continue
                            captures, seen = self._harvest(page, context, harvest_db, keep_session=False, job=None)
                            _maybe_save_landed(context, harvest_db, _latest_page(context, page))
                            _save_capture(harvest_db, captures, seen)
                            hint = _shop_name(_latest_page(context, page))
                            payload = normalize_captures(captures, shop_hint=hint or "拼多多店铺", today=today)
                            box.put((True, payload))
                            continue
                        box.put((False, AppError("未知浏览器指令", status=500, code="browser_failed")))
                    except Exception as exc:
                        box.put((False, exc if isinstance(exc, AppError) else _browser_error(exc)))
        except Exception as exc:
            holder["error"] = exc if isinstance(exc, AppError) else _browser_error(exc)
            if not ready.is_set():
                ready.set()
        finally:
            self._drain_hold(commands)
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            release_browser()
            if self._hold_thread is threading.current_thread():
                self._hold_thread = None

    def _drain_hold(self, commands: queue.Queue) -> None:
        while True:
            try:
                _command, box = commands.get_nowait()
            except queue.Empty:
                return
            try:
                box.put((False, AppError("浏览器已关闭", status=409, code="browser_closed")))
            except Exception:
                return

    def _browse(
        self,
        db_path,
        *,
        job,
        today: date,
        wait_seconds: int,
        restore: bool,
        harvest: bool = True,
        open_url: str | None = None,
        establish_session: bool = True,
    ):
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
                        if not _has_login_cookie(context) and not _has_yingxiao_session(context):
                            from app.services.live_login import drop_live_login

                            drop_live_login(db_path)
                            raise AppError(MMS_LOGIN_NOTE, status=401, code="login_required")
                    else:
                        if not self._wait_for_scan(page, job, db_path, wait_seconds, open_url or self.login_url):
                            return None
                        if _cancelled(job) and not job.snapshot().get("cookies_saved"):
                            return None
                        _save_storage(context, db_path)
                        from app.services.live_login import finish_live_session

                        hint = _shop_name(page)
                        job.update(cookies_saved=True, message=SAVED_LOGIN_NOTE)
                        if not establish_session:
                            job.update(status="confirmed", finished=True, cookies_saved=True, message=SAVED_LOGIN_NOTE)
                            return None
                        provisional = normalize_captures([], shop_hint=hint or "拼多多店铺", today=today)
                        provisional.note = SAVED_LOGIN_NOTE
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
                            message=SAVED_LOGIN_NOTE,
                        )
                        return None
                    if not harvest:
                        return None
                    hint = _shop_name(page)
                    try:
                        captures, seen = self._harvest(
                            page,
                            context,
                            db_path,
                            keep_session=job is not None,
                            job=job,
                        )
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

    def _wait_for_scan(self, page, job, db_path: Path, wait_seconds: int, open_url: str) -> bool:
        import time

        try:
            page.goto(open_url, wait_until="domcontentloaded", timeout=20000)
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
                job.update(qr_image=image, message="请在弹出的浏览器里自己完成登录，然后回到推广台点「同步」。")
            if _logged_in(page.context, page) or _has_yingxiao_session(page.context):
                job.update(message=SAVED_LOGIN_NOTE)
                return True
            try:
                page.wait_for_url(lambda url: "/login" not in (url or ""), timeout=400)
            except Exception:
                page.wait_for_timeout(200)
        from app.services.live_login import mark_ticket

        mark_ticket(db_path, job.ticket, "expired")
        job.update(status="expired", message="登录等待超时。请重新打开登录页，在浏览器里自己完成登录后再点「同步」。")
        return False

    def _open_first(self, page) -> None:
        target = self.report_urls[0] if self.report_urls else self.login_url
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            raise _browser_error(exc) from exc

    def _harvest(self, page, context, db_path: Path, keep_session: bool = False, job=None):
        bucket: list[dict] = []
        seen: list[dict] = []
        stuck = False
        stuck_gate = ""

        def on_response(response) -> None:
            _take_response(response, bucket, seen)

        context.on("response", on_response)
        for url in self.report_urls:
            if not should_open_report_url(url, _cookie_records(context)):
                stuck = True
                stuck_gate = stuck_gate or "推广中心"
                continue
            if stuck and (is_sso_entry(url) or yingxiao_host(url)):
                continue
            active = _latest_page(context, page)
            try:
                active.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            _settle(active)
            active = _latest_page(context, active)
            _capture_home_spend(active, bucket)
            gate = _auth_gate_label(_page_url(active))
            if gate == "商家后台" and not _has_login_cookie(context):
                if _has_yingxiao_session(context):
                    continue
                if keep_session:
                    return bucket, seen
                from app.services.live_login import drop_live_login

                drop_live_login(db_path)
                raise AppError(MMS_LOGIN_NOTE, status=401, code="login_required")
            if gate:
                stuck = True
                stuck_gate = gate
                continue
            if (promo_surface_url(_page_url(active)) or _has_yingxiao_session(context)) and not _auth_blocks_promo(bucket):
                _save_storage(context, db_path)
                stuck = False
            active = _open_report_dimensions(context, active, bucket, db_path)
            if _auth_gate_label(_page_url(active)):
                stuck = True
                stuck_gate = _auth_gate_label(_page_url(active))
                continue
            if promo_surface_url(_page_url(active)) and not _auth_blocks_promo(bucket):
                stuck = False
        if not bucket_has_metrics(bucket) and (stuck or bucket_auth_failed(bucket) or bucket_token_invalid(bucket)):
            if bucket_token_invalid(bucket):
                _append_note(bucket, TOKEN_INVALID_NOTE)
            elif stuck_gate == "商家后台":
                _append_note(bucket, MMS_LOGIN_NOTE)
            else:
                _append_note(bucket, AUTH_FAILED_NOTE)
        else:
            note = guidance_note(bucket, seen)
            if note:
                _append_note(bucket, note)
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


def load_saved_cookies(db_path: Path) -> list[dict]:
    path = storage_state_path(db_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return []
    return [item for item in cookies if isinstance(item, dict)]


def _playwright_cookie(item: dict) -> dict | None:
    name = str(item.get("name") or "")
    domain = str(item.get("domain") or "")
    if not name or item.get("value") is None or not domain:
        return None
    cookie = {
        "name": name,
        "value": str(item.get("value")),
        "domain": domain,
        "path": str(item.get("path") or "/"),
    }
    expires = item.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = float(expires)
    if "httpOnly" in item:
        cookie["httpOnly"] = bool(item["httpOnly"])
    if "secure" in item:
        cookie["secure"] = bool(item["secure"])
    same = item.get("sameSite")
    if same in {"Strict", "Lax", "None"}:
        cookie["sameSite"] = same
    return cookie


def _prepare_manual_context(context, url: str, db_path: Path) -> None:
    """打开登录页前清掉窗口里的 Cookie，只放回已经能用的推广中心会话。"""
    try:
        context.clear_cookies()
    except Exception:
        pass
    batch = []
    for item in cookies_for_manual_open(url, load_saved_cookies(db_path)):
        cookie = _playwright_cookie(item)
        if cookie is not None:
            batch.append(cookie)
    if not batch:
        return
    try:
        context.add_cookies(batch)
    except Exception:
        return


def _maybe_save_landed(context, db_path: Path, page) -> None:
    if should_persist_manual_session(_page_url(page), _cookie_records(context)):
        _save_storage(context, db_path)


def _json_chars(body) -> int:
    try:
        return len(json.dumps(body, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def _essential_json(body) -> bool:
    return bool(json_has_metrics(body) or body_auth_failed(body) or _tree_has_key(body, {"yesdata", "todaydata", "yeslist", "todaylist"}))


def _identity_slice(body) -> dict | None:
    found: dict[str, object] = {}

    def walk(node, depth: int) -> None:
        if depth > 6 or ("id" in found and "name" in found):
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if value in (None, ""):
                    continue
                norm = str(key).lower().replace("-", "_")
                if norm in {"mall_id", "mallid"} and "id" not in found:
                    found["id"] = value
                elif norm in {"mall_name", "mallname"} and "name" not in found:
                    found["name"] = value
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)

    walk(body, 0)
    if not found:
        return None
    slim: dict[str, object] = {}
    if "id" in found:
        slim["mallId"] = found["id"]
    if "name" in found:
        slim["mallName"] = found["name"]
    return slim


def _save_capture(db_path: Path, captures: list[dict], seen: list[dict] | None = None) -> None:
    debug = {"kind": "debug", "urls": list(seen or [])[:_MAX_URLS]}
    preview = [debug]
    for item in captures:
        kind = item.get("kind")
        if kind == "json":
            body = item.get("body")
            if _json_chars(body) > _MAX_JSON_CHARS and not _essential_json(body):
                body = _identity_slice(body)
                if not body:
                    continue
            base, path = split_response_url(item.get("url") or "")
            preview.append(
                {
                    "kind": "json",
                    "url": base,
                    "path": item.get("path") or path,
                    "promo": bool(item.get("promo")),
                    "body": body,
                }
            )
        elif kind == "note":
            preview.append({"kind": "note", "body": item.get("body") or ""})
        else:
            body = item.get("body")
            size = len(body) if isinstance(body, (bytes, bytearray)) else None
            preview.append(
                {
                    "kind": kind,
                    "url": item.get("url") or "",
                    "path": item.get("path") or "",
                    "promo": bool(item.get("promo", True)),
                    "bytes": size,
                }
            )
    raw = json.dumps(preview, ensure_ascii=False, default=str)
    while len(raw) > 200_000 and len(preview) > 1:
        drop_at = _drop_index(preview)
        preview.pop(drop_at)
        raw = json.dumps(preview, ensure_ascii=False, default=str)
    write_private_text(capture_path(db_path), raw)


def _drop_index(preview: list[dict]) -> int:
    """先丢掉又大又不是报表的正文。调试地址清单留在最前面。"""
    best = 0
    best_size = -1
    for index, item in enumerate(preview):
        if index == 0 or item.get("kind") == "note":
            continue
        body = item.get("body")
        if item.get("kind") == "json" and _essential_json(body):
            continue
        size = _json_chars(body) if item.get("kind") == "json" else int(item.get("bytes") or 0)
        if size >= best_size:
            best = index
            best_size = size
    if best:
        return best
    return len(preview) - 1


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


def _is_goods_effect(url: str) -> bool:
    return "goods_effect" in (url or "") or "goodsDataShow" in (url or "")


def _page_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _context_urls(context, page) -> list[str]:
    urls = []
    if page is not None:
        urls.append(_page_url(page))
    try:
        pages = [item for item in context.pages if not item.is_closed()]
    except Exception:
        pages = []
    for item in pages:
        urls.append(_page_url(item))
    return urls


def _cookie_records(context) -> list[dict]:
    try:
        return list(context.cookies())
    except Exception:
        return []


def _has_yingxiao_session(context) -> bool:
    return yingxiao_session_ready(_cookie_records(context))


def _context_has_page(context) -> bool:
    try:
        return any(not item.is_closed() for item in context.pages)
    except Exception:
        return False


def _page_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _capture_home_spend(page, bucket: list[dict]) -> None:
    if promo_surface_url(_page_url(page)) or on_sso_page(_page_url(page)):
        return
    try:
        text = page.inner_text("body", timeout=1000)
    except Exception:
        return
    today_amount, yesterday_amount = parse_home_promo_spend_text(text or "")
    if not today_amount and not yesterday_amount:
        return
    base, path = split_response_url(_page_url(page))
    bucket.append(
        {
            "kind": "json",
            "url": base,
            "path": path,
            "promo": False,
            "role": "home-spend",
            "body": {
                "source": "mms-home-ui",
                "todayPromoSpend": today_amount,
                "yesPromoSpend": yesterday_amount,
            },
        }
    )


def _append_note(bucket: list[dict], text: str) -> None:
    if any(item.get("kind") == "note" and text in str(item.get("body") or "") for item in bucket):
        return
    bucket.append({"kind": "note", "body": text})


def _auth_gate_label(url: str) -> str:
    """当前页是登录门时返回要打开的页面名。同步见到它就停，不代点。"""
    if on_sso_page(url) or (yingxiao_host(url) and "/login" in (url or "")):
        return "推广中心"
    path = urlsplit(url or "").path or ""
    if "/login" in path:
        return "商家后台"
    return ""


def _open_report_dimensions(context, active, bucket: list[dict], db_path: Path):
    """概况页不够。报表页上再点商品、创意、主图，并选一个已有日期。登录页不点。"""
    if _auth_gate_label(_page_url(active)):
        return active
    if not bucket_has_metrics(bucket) and not promo_surface_url(_page_url(active)):
        _click_exact(active, REPORT_NAV)
        active = _latest_page(context, active)
        _settle(active)
        if promo_surface_url(_page_url(active)) or (yingxiao_host(_page_url(active)) and not on_sso_page(_page_url(active))):
            _save_storage(context, db_path)
    if promo_surface_url(_page_url(active)):
        _save_storage(context, db_path)
        if not bucket_has_creative(bucket):
            _click_each(active, DIMENSION_LABELS)
            active = _latest_page(context, active)
            _settle(active)
        if not bucket_has_metrics(bucket):
            _click_exact(active, DATE_LABELS)
            active = _latest_page(context, active)
            _settle(active)
    elif not bucket_has_metrics(bucket):
        _click_exact(active, DATE_LABELS)
        active = _latest_page(context, active)
        _settle(active)
    _click_exports(active, bucket)
    return active


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
        if _click_label(page, label):
            return True
    return False


def _click_each(page, labels: tuple[str, ...]) -> None:
    for label in labels:
        if _click_label(page, label):
            _settle(page)


def _click_label(page, label: str) -> bool:
    if not (is_promo_nav_label(label) or is_date_preset_label(label) or is_dimension_label(label)):
        return False
    try:
        loc = page.get_by_text(label, exact=True)
        count = min(loc.count(), 4)
    except Exception:
        return False
    for index in range(count):
        node = loc.nth(index)
        try:
            if not node.is_visible():
                continue
            node.click(timeout=1200, no_wait_after=True)
        except Exception:
            continue
        try:
            popup = page.wait_for_event("popup", timeout=800)
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
            essential = _essential_json(body)
            item = {"kind": "json", "url": base, "path": path, "body": body, "promo": promo or essential}
            if _json_chars(body) > _MAX_JSON_CHARS and not essential:
                ident = _identity_slice(body)
                if ident and not any(entry.get("role") == "identity" for entry in bucket):
                    bucket.append(
                        {
                            "kind": "json",
                            "url": base,
                            "path": path,
                            "body": ident,
                            "promo": False,
                            "role": "identity",
                        }
                    )
                return
            if promo or essential:
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
        url = old.get("url") or ""
        if "yingxiao.pinduoduo.com" in url or _essential_json(old.get("body")):
            continue
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
                base, path = split_response_url(_page_url(page))
                bucket.append({"kind": kind, "url": base, "path": path, "body": data, "promo": True})
            clicked += 1
        except Exception:
            continue
