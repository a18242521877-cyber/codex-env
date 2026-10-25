"""启动本机服务，并尽量打开 Windows 桌面窗口。"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from app.config import APP_NAME, DEFAULT_HOST, default_db_path, default_port
from app.db import connect, init_db
from app.scheduler import Scheduler
from app.server import AppServer

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="推广台：拼多多店铺推广、测图、测款监控")
    parser.add_argument("--browser", action="store_true", help="用系统浏览器打开，不嵌桌面窗口")
    parser.add_argument("--serve", action="store_true", help="只启动本机服务，不打开界面")
    parser.add_argument("--self-test", action="store_true", help="跑一遍内置自检后退出")
    parser.add_argument("--port", type=int, default=None, help=f"端口，默认 {default_port()}")
    parser.add_argument("--db", default=None, help="SQLite 路径，默认在本机用户目录")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        from app.selftest import run_self_test

        return run_self_test()
    db_path = Path(args.db).expanduser() if args.db else default_db_path()
    port = args.port if args.port is not None else default_port()
    if args.serve:
        mode = "serve"
    elif args.browser:
        mode = "browser"
    else:
        mode = "auto"
    return serve(db_path, port, mode)


def serve(db_path: Path, port: int, mode: str) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    try:
        httpd = AppServer((DEFAULT_HOST, port), str(db_path))
    except OSError as exc:
        print(f"端口 {port} 无法监听：{exc}", file=sys.stderr)
        print("可以换一个端口：python run.py --port 8766", file=sys.stderr)
        return 1
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/"
    scheduler = Scheduler(db_path)
    scheduler.start()
    thread = threading.Thread(target=httpd.serve_forever, name="pdd-http", daemon=True)
    thread.start()
    print(f"{APP_NAME}已启动：{url}")
    print(f"数据文件：{db_path}")
    stopped = False

    def stop() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        scheduler.stop()
        httpd.shutdown()
        httpd.server_close()

    try:
        if mode == "auto":
            if _webview_available():
                mode = "webview"
            else:
                mode = "browser"
                print("未检测到 pywebview，已改用系统浏览器。")
                print("Windows 上若要独立窗口：pip install -r requirements-windows.txt")
        if mode == "webview":
            _open_webview(url, stop)
        elif mode == "browser":
            webbrowser.open(url)
            print("按 Ctrl+C 停止。")
            _wait(thread)
        else:
            print("按 Ctrl+C 停止。")
            _wait(thread)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        stop()
    return 0


def _wait(thread: threading.Thread) -> None:
    while thread.is_alive():
        thread.join(0.5)


def _webview_available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def _open_webview(url: str, stop) -> None:
    import webview

    window = webview.create_window(APP_NAME, url, width=1280, height=860, min_size=(1080, 700))
    try:
        window.events.closed += stop
    except Exception:
        logger.info("当前 pywebview 不支持关闭事件，窗口关掉后进程会跟着退出")
    webview.start()


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass
