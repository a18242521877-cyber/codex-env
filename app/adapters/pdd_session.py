"""本机会话文件。只放在数据库旁边，退出登录时删掉。"""

from __future__ import annotations

import os
from pathlib import Path


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
