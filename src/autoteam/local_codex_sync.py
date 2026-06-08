"""Sync one active pool account into the local Codex CLI auth.json."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from autoteam.accounts import STATUS_ACTIVE, is_account_disabled, load_accounts
from autoteam.config import LOCAL_CODEX_AUTH_PATH
from autoteam.textio import read_text


def resolve_local_codex_auth_path(path: str | None = None) -> Path:
    value = (path or LOCAL_CODEX_AUTH_PATH or "~/.codex/auth.json").strip()
    return Path(os.path.expandvars(os.path.expanduser(value)))


def codex_cli_auth_from_bundle(bundle: dict) -> dict:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": bundle.get("id_token", ""),
            "access_token": bundle.get("access_token", ""),
            "refresh_token": bundle.get("refresh_token", ""),
            "account_id": bundle.get("account_id", ""),
        },
        "last_refresh": bundle.get("last_refresh") or int(time.time()),
    }


def write_local_codex_auth(auth_file: str | Path, *, dest: str | Path | None = None) -> dict:
    src = Path(auth_file)
    if not src.exists():
        return {"ok": False, "error": f"auth_file_not_found:{src}"}

    auth_data = json.loads(read_text(src))
    payload = codex_cli_auth_from_bundle(auth_data)
    target = Path(dest) if dest is not None else resolve_local_codex_auth_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    backup = None
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            backup.chmod(0o600)
        except Exception:
            pass

    fd, tmp_name = tempfile.mkstemp(prefix=".auth.", suffix=".json", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.chmod(0o600)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        target.chmod(0o600)
    except Exception:
        pass

    return {
        "ok": True,
        "target": str(target),
        "source": str(src),
        "backup": str(backup) if backup else None,
        "account_id": payload["tokens"].get("account_id"),
    }


def _active_sort_key(acc: dict) -> tuple:
    quota = acc.get("last_quota") if isinstance(acc.get("last_quota"), dict) else {}
    primary_pct = quota.get("primary_pct")
    if not isinstance(primary_pct, (int, float)):
        primary_pct = 999
    last_active_at = acc.get("last_active_at")
    if not isinstance(last_active_at, (int, float)):
        last_active_at = 0
    return (int(primary_pct), -float(last_active_at), str(acc.get("email") or ""))


def select_best_active_account() -> dict | None:
    candidates = []
    for acc in load_accounts():
        if acc.get("status") != STATUS_ACTIVE:
            continue
        if is_account_disabled(acc):
            continue
        auth_file = (acc.get("auth_file") or "").strip()
        if not auth_file or not Path(auth_file).exists():
            continue
        candidates.append(acc)
    if not candidates:
        return None
    candidates.sort(key=_active_sort_key)
    return candidates[0]


def sync_best_active_to_local_codex() -> dict:
    acc = select_best_active_account()
    if not acc:
        return {"ok": False, "skipped": True, "reason": "no_active_account"}
    result = write_local_codex_auth(acc["auth_file"])
    return {"email": acc.get("email"), **result}


def sync_account_to_local_codex(email: str, filepath: str) -> dict:
    from autoteam.accounts import find_account

    normalized = (email or "").strip().lower()
    account = find_account(load_accounts(), normalized)
    if not account:
        return {"ok": False, "skipped": True, "reason": "account_missing"}
    if account.get("status") != STATUS_ACTIVE:
        return {"ok": False, "skipped": True, "reason": "account_not_active", "status": account.get("status")}
    if is_account_disabled(account):
        return {"ok": False, "skipped": True, "reason": "account_disabled"}
    result = write_local_codex_auth(filepath)
    return {"email": normalized, **result}
