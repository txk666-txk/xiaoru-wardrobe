r"""安全清理指定用户的旧对话记忆，保留授权和数据库结构。

用法：
    .\.venv\python.exe scripts\reset_ecommerce_memory.py --user User --apply

执行前请停止 Fay，避免运行中的定时保存线程把旧记忆重新写回。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = PROJECT_ROOT / "memory"
SAFE_USER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validated_user_dir(user: str) -> Path:
    if not SAFE_USER_PATTERN.fullmatch(user) or user in {".", ".."} or ".." in user:
        raise ValueError("用户名只能包含字母、数字、下划线、点和连字符，且不能包含路径跳转")
    candidate = (MEMORY_ROOT / user).resolve()
    memory_root = MEMORY_ROOT.resolve()
    if candidate.parent != memory_root:
        raise ValueError("目标记忆目录不在 memory/<user> 范围内")
    return candidate


def _fay_is_listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", 5000)) == 0


def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(con.execute(sql, params).fetchone()[0])


def inspect(user: str) -> dict:
    fay_db = MEMORY_ROOT / "fay.db"
    profile_db = MEMORY_ROOT / "user_profiles.db"
    user_dir = _validated_user_dir(user)
    nodes_path = user_dir / "memory_stream" / "nodes.json"
    embeddings_path = user_dir / "memory_stream" / "embeddings.json"

    summary: dict[str, object] = {
        "user": user,
        "message_count": 0,
        "adopted_count": 0,
        "authorize_count": 0,
        "profile_count": 0,
        "nodes_count": 0,
        "embeddings_count": 0,
        "embedding_backups": [],
    }

    if fay_db.exists():
        with sqlite3.connect(fay_db) as con:
            summary["message_count"] = _count(con, "SELECT COUNT(*) FROM T_Msg WHERE username = ?", (user,))
            summary["adopted_count"] = _count(
                con,
                "SELECT COUNT(*) FROM T_Adopted WHERE msg_id IN (SELECT id FROM T_Msg WHERE username = ?)",
                (user,),
            )
            summary["authorize_count"] = _count(con, "SELECT COUNT(*) FROM T_Authorize")

    if profile_db.exists():
        with sqlite3.connect(profile_db) as con:
            summary["profile_count"] = _count(con, "SELECT COUNT(*) FROM T_Member WHERE username = ?", (user,))

    if nodes_path.exists():
        with nodes_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
            summary["nodes_count"] = len(data) if isinstance(data, list) else 0

    if embeddings_path.exists():
        with embeddings_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
            summary["embeddings_count"] = len(data) if isinstance(data, dict) else 0

    stream_dir = user_dir / "memory_stream"
    if stream_dir.exists():
        summary["embedding_backups"] = [str(path.relative_to(PROJECT_ROOT)) for path in stream_dir.glob("embeddings*.bak")]

    return summary


def reset(user: str) -> None:
    fay_db = MEMORY_ROOT / "fay.db"
    profile_db = MEMORY_ROOT / "user_profiles.db"
    user_dir = _validated_user_dir(user)
    stream_dir = user_dir / "memory_stream"
    nodes_path = stream_dir / "nodes.json"
    embeddings_path = stream_dir / "embeddings.json"

    if fay_db.exists():
        with sqlite3.connect(fay_db, timeout=30) as con:
            con.execute("PRAGMA secure_delete = ON")
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "DELETE FROM T_Adopted WHERE msg_id IN (SELECT id FROM T_Msg WHERE username = ?)",
                (user,),
            )
            con.execute("DELETE FROM T_Msg WHERE username = ?", (user,))
            con.commit()
            con.execute("VACUUM")

    if profile_db.exists():
        with sqlite3.connect(profile_db, timeout=30) as con:
            con.execute("PRAGMA secure_delete = ON")
            con.execute(
                "UPDATE T_Member SET extra_info = '', user_portrait = '' WHERE username = ?",
                (user,),
            )
            con.commit()
            con.execute("VACUUM")

    stream_dir.mkdir(parents=True, exist_ok=True)
    nodes_path.write_text("[]\n", encoding="utf-8")
    embeddings_path.write_text("{}\n", encoding="utf-8")
    for path in stream_dir.glob("embeddings*.bak"):
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="安全清理 Fay 指定用户的旧记忆")
    parser.add_argument("--user", default="User", help="要清理的用户名，默认 User")
    parser.add_argument("--apply", action="store_true", help="实际执行；不提供时仅检查")
    parser.add_argument("--force-online", action="store_true", help="允许 Fay 运行时清理（不推荐）")
    args = parser.parse_args()

    before = inspect(args.user)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "before": before}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0

    if _fay_is_listening() and not args.force_online:
        raise RuntimeError("检测到 Fay 正在监听 5000 端口。请先停止 Fay，避免旧记忆被重新写回。")

    reset(args.user)
    after = inspect(args.user)
    print(json.dumps({"after": after}, ensure_ascii=False, indent=2))

    if after["authorize_count"] != before["authorize_count"]:
        raise RuntimeError("授权记录数量发生变化，已停止后续操作")
    if any(after[key] for key in ("message_count", "adopted_count", "nodes_count", "embeddings_count")):
        raise RuntimeError("仍有旧记忆未清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
