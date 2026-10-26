"""本机会话文件。只放在数据库旁边，退出登录时删掉。"""

from __future__ import annotations

import json
import os
from pathlib import Path

PASS_COOKIE_NAMES = {"PASS_ID", "SUB_PASS_ID"}


def session_dir(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve().parent / "pdd-session"


def storage_state_path(db_path: str | Path) -> Path:
    return session_dir(db_path) / "storage_state.json"


def capture_path(db_path: str | Path) -> Path:
    return session_dir(db_path) / "last_capture.json"


def write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def tighten(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_shop_token_cookie(name: str) -> bool:
    return str(name).startswith("windows_app_shop_token")


def cookie_names_from_state(data: dict) -> set[str]:
    names: set[str] = set()
    for item in data.get("cookies") or []:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


def page_looks_logged_in(names: set[str], url: str) -> bool:
    """离开登录页，并且已经有商家 Cookie。

    只停留在 /login 时不算成功。api_uid 在扫码前就会出现，必须配合已经离开登录页。
    PASS_ID 仍然是最稳的信号，不要求一定有 SUB_PASS_ID。
    """
    if "/login" in (url or ""):
        return False
    if names & PASS_COOKIE_NAMES:
        return True
    if any(is_shop_token_cookie(name) for name in names):
        return True
    return "api_uid" in names


def storage_state_is_logged_in(path: Path) -> bool:
    """磁盘上的登录文件。api_uid 单独出现不算已登录，避免把扫码前的文件当成会话。"""
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    names = cookie_names_from_state(data)
    if names & PASS_COOKIE_NAMES:
        return True
    return any(is_shop_token_cookie(name) for name in names)


def remove_session_files(db_path: str | Path) -> None:
    folder = session_dir(db_path)
    for path in (storage_state_path(db_path), capture_path(db_path)):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
