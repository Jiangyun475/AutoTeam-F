"""统一远端同步目标分发：CPA / Sub2API。"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from autoteam.textio import parse_env_value

logger = logging.getLogger(__name__)

SYNC_TARGET_CPA = "cpa"
SYNC_TARGET_SUB2API = "sub2api"
SYNC_TARGET_LOCAL_CODEX = "local_codex"

_SYNC_TARGET_META = {
    SYNC_TARGET_CPA: {
        "label": "CPA",
        "toggle_key": "SYNC_TARGET_CPA",
        "config_keys": ("CPA_URL", "CPA_KEY"),
    },
    SYNC_TARGET_SUB2API: {
        "label": "Sub2API",
        "toggle_key": "SYNC_TARGET_SUB2API",
        "config_keys": ("SUB2API_URL", "SUB2API_EMAIL", "SUB2API_PASSWORD"),
    },
    SYNC_TARGET_LOCAL_CODEX: {
        "label": "本机 Codex CLI",
        "toggle_key": "SYNC_TARGET_LOCAL_CODEX",
        "config_keys": (),
    },
}

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def _normalize_env(env: Mapping[str, object] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {str(key): "" if value is None else str(value) for key, value in source.items()}


def parse_bool_env(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = parse_env_value(str(value))
    if not text:
        return default
    return text.strip().lower() in _TRUE_VALUES


def get_sync_target_meta(target: str) -> dict[str, object]:
    try:
        return _SYNC_TARGET_META[target]
    except KeyError as exc:
        raise KeyError(f"未知同步目标: {target}") from exc


def get_sync_target_states(env: Mapping[str, object] | None = None) -> dict[str, bool]:
    values = _normalize_env(env)
    states = {}
    for target, meta in _SYNC_TARGET_META.items():
        toggle_key = str(meta["toggle_key"])
        config_keys = tuple(meta["config_keys"])
        raw_toggle = (values.get(toggle_key) or "").strip()
        if raw_toggle:
            states[target] = parse_bool_env(raw_toggle)
        elif not config_keys:
            states[target] = False
        else:
            states[target] = all((values.get(key) or "").strip() for key in config_keys)
    return states


def is_sync_target_enabled(target: str, env: Mapping[str, object] | None = None) -> bool:
    return get_sync_target_states(env).get(target, False)


def get_enabled_sync_targets(env: Mapping[str, object] | None = None) -> list[str]:
    states = get_sync_target_states(env)
    return [target for target in _SYNC_TARGET_META if states.get(target)]


def get_available_sync_targets(env: Mapping[str, object] | None = None) -> list[str]:
    values = _normalize_env(env)
    available = []
    for target, meta in _SYNC_TARGET_META.items():
        if all((values.get(key) or "").strip() for key in tuple(meta["config_keys"])):
            available.append(target)
    return available


def get_sync_target_labels(targets: list[str] | None = None) -> list[str]:
    if targets is None:
        targets = list(_SYNC_TARGET_META)
    labels = []
    for target in targets:
        meta = _SYNC_TARGET_META.get(target)
        if meta:
            labels.append(str(meta["label"]))
    return labels


def describe_sync_targets(targets: list[str] | None = None) -> str:
    labels = get_sync_target_labels(targets)
    if not labels:
        return "未启用远端同步目标"
    return " + ".join(labels)


def get_missing_target_configs(
    targets: list[str] | None = None, env: Mapping[str, object] | None = None
) -> list[tuple[str, str]]:
    values = _normalize_env(env)
    missing: list[tuple[str, str]] = []
    for target in targets or []:
        meta = get_sync_target_meta(target)
        for key in tuple(meta["config_keys"]):
            value = (values.get(key) or "").strip()
            if not value:
                missing.append((key, str(meta["label"])))
    return missing


def sync_to_configured_targets():
    results = {}
    enabled_targets = get_enabled_sync_targets()

    if SYNC_TARGET_CPA in enabled_targets:
        from autoteam.cpa_sync import sync_to_cpa

        try:
            results[SYNC_TARGET_CPA] = sync_to_cpa()
        except Exception as exc:
            logger.warning("[Sync] CPA 同步失败，保留本地轮换结果: %s", exc)
            results[SYNC_TARGET_CPA] = {"ok": False, "error": str(exc)}

    if SYNC_TARGET_SUB2API in enabled_targets:
        from autoteam.sub2api_sync import sync_to_sub2api

        try:
            results[SYNC_TARGET_SUB2API] = sync_to_sub2api()
        except Exception as exc:
            logger.warning("[Sync] Sub2API 同步失败，保留本地轮换结果: %s", exc)
            results[SYNC_TARGET_SUB2API] = {"ok": False, "error": str(exc)}

    if SYNC_TARGET_LOCAL_CODEX in enabled_targets:
        from autoteam.local_codex_sync import sync_best_active_to_local_codex

        try:
            results[SYNC_TARGET_LOCAL_CODEX] = sync_best_active_to_local_codex()
        except Exception as exc:
            logger.warning("[Sync] 本机 Codex CLI 同步失败，保留本地轮换结果: %s", exc)
            results[SYNC_TARGET_LOCAL_CODEX] = {"ok": False, "error": str(exc)}

    return results


def sync_account_to_configured_targets(email: str, filepath: str):
    """只把一个已就绪的账号凭证同步到已启用目标，不触发远端清理。"""
    from autoteam.accounts import STATUS_ACTIVE, find_account, is_account_disabled, load_accounts

    normalized_email = (email or "").strip().lower()
    auth_path = Path(filepath)
    if not normalized_email:
        return {"ok": False, "skipped": True, "reason": "missing_email"}
    if not auth_path.exists():
        logger.warning("[Sync] 新凭证文件不存在，跳过即时同步: %s (%s)", normalized_email, auth_path)
        return {"ok": False, "skipped": True, "reason": "missing_auth_file", "auth_file": str(auth_path)}

    account = find_account(load_accounts(), normalized_email)
    if not account:
        logger.warning("[Sync] 未找到账号记录，跳过新凭证即时同步: %s", normalized_email)
        return {"ok": False, "skipped": True, "reason": "account_missing", "auth_file": auth_path.name}
    if is_account_disabled(account):
        logger.info("[Sync] 账号已禁用，跳过新凭证即时同步: %s", normalized_email)
        return {"ok": False, "skipped": True, "reason": "account_disabled", "auth_file": auth_path.name}
    if account.get("status") != STATUS_ACTIVE:
        logger.info(
            "[Sync] 账号尚未 active，跳过新凭证即时同步: %s (status=%s)",
            normalized_email,
            account.get("status"),
        )
        return {
            "ok": False,
            "skipped": True,
            "reason": "account_not_active",
            "status": account.get("status"),
            "auth_file": auth_path.name,
        }

    account_auth = account.get("auth_file")
    if account_auth and Path(account_auth).name != auth_path.name:
        logger.info(
            "[Sync] 请求同步的凭证与账号当前 auth_file 不同，仍按请求文件上传: %s (%s -> %s)",
            normalized_email,
            Path(account_auth).name,
            auth_path.name,
        )

    results = {}
    enabled_targets = get_enabled_sync_targets()

    if SYNC_TARGET_CPA in enabled_targets:
        from autoteam.cpa_sync import upload_to_cpa

        try:
            uploaded = upload_to_cpa(auth_path)
            results[SYNC_TARGET_CPA] = {"ok": bool(uploaded), "uploaded": auth_path.name}
        except Exception as exc:
            logger.warning("[Sync] CPA 新凭证即时同步失败，保留本地结果: %s", exc)
            results[SYNC_TARGET_CPA] = {"ok": False, "error": str(exc), "uploaded": auth_path.name}

    if SYNC_TARGET_SUB2API in enabled_targets:
        from autoteam.sub2api_sync import sync_account_to_sub2api

        try:
            quota_info = account.get("last_quota") if isinstance(account.get("last_quota"), dict) else None
            target_result = sync_account_to_sub2api(normalized_email, str(auth_path), quota_info=quota_info)
            results[SYNC_TARGET_SUB2API] = {"ok": True, **target_result}
        except Exception as exc:
            logger.warning("[Sync] Sub2API 新凭证即时同步失败，保留本地结果: %s", exc)
            results[SYNC_TARGET_SUB2API] = {"ok": False, "error": str(exc), "uploaded": auth_path.name}

    if SYNC_TARGET_LOCAL_CODEX in enabled_targets:
        from autoteam.local_codex_sync import sync_account_to_local_codex

        try:
            results[SYNC_TARGET_LOCAL_CODEX] = sync_account_to_local_codex(normalized_email, str(auth_path))
        except Exception as exc:
            logger.warning("[Sync] 本机 Codex CLI 新凭证即时同步失败，保留本地结果: %s", exc)
            results[SYNC_TARGET_LOCAL_CODEX] = {"ok": False, "error": str(exc), "uploaded": auth_path.name}

    if not results:
        logger.info("[Sync] 未启用远端同步目标，跳过新凭证即时同步: %s", normalized_email)
        return {"ok": True, "skipped": True, "reason": "no_enabled_targets", "auth_file": auth_path.name}

    return {
        "ok": all(item.get("ok") for item in results.values()),
        "auth_file": auth_path.name,
        "targets": results,
    }


def sync_main_codex_to_configured_targets(filepath: str):
    results = {}
    enabled_targets = get_enabled_sync_targets()

    if SYNC_TARGET_CPA in enabled_targets:
        from autoteam.cpa_sync import sync_main_codex_to_cpa

        results[SYNC_TARGET_CPA] = sync_main_codex_to_cpa(filepath)

    if SYNC_TARGET_SUB2API in enabled_targets:
        from autoteam.sub2api_sync import sync_main_codex_to_sub2api

        results[SYNC_TARGET_SUB2API] = sync_main_codex_to_sub2api(filepath)

    return results


def delete_main_codex_from_configured_targets(*, include_disabled: bool = False):
    results = {}
    targets = get_available_sync_targets() if include_disabled else get_enabled_sync_targets()

    if SYNC_TARGET_CPA in targets:
        from autoteam.cpa_sync import delete_main_codex_from_cpa

        results[SYNC_TARGET_CPA] = delete_main_codex_from_cpa()

    if SYNC_TARGET_SUB2API in targets:
        from autoteam.sub2api_sync import delete_main_codex_from_sub2api

        results[SYNC_TARGET_SUB2API] = delete_main_codex_from_sub2api()

    return results


def delete_account_from_configured_targets(
    email: str, *, auth_names: list[str] | None = None, include_disabled: bool = False
):
    results = {}
    targets = get_available_sync_targets() if include_disabled else get_enabled_sync_targets()

    if SYNC_TARGET_CPA in targets:
        from autoteam.cpa_sync import delete_from_cpa, list_cpa_files

        deleted = []
        auth_name_set = set(auth_names or [])
        for item in list_cpa_files():
            item_email = (item.get("email") or "").lower()
            item_name = item.get("name") or ""
            if item_email == email.lower() or item_name in auth_name_set:
                if delete_from_cpa(item_name):
                    deleted.append(item_name)
        results[SYNC_TARGET_CPA] = {"deleted": deleted, "count": len(deleted)}

    if SYNC_TARGET_SUB2API in targets:
        from autoteam.sub2api_sync import delete_account_from_sub2api

        results[SYNC_TARGET_SUB2API] = delete_account_from_sub2api(email, auth_names=auth_names or [])

    return results
