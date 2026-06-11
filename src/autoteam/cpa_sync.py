"""CPA (CLIProxyAPI) 认证文件同步 - 保持本地 codex 认证文件与 CPA 一致"""

import base64
import json
import logging
import time
from datetime import datetime
from hashlib import md5
from pathlib import Path

import requests

from autoteam.auth_storage import AUTH_DIR, ensure_auth_dir, ensure_auth_file_permissions
from autoteam.config import AUTO_CHECK_TARGET_SEATS, CPA_KEY, CPA_URL
from autoteam.textio import write_text

logger = logging.getLogger(__name__)


_CPA_SESSION = requests.Session()
_CPA_SESSION.trust_env = False

_TRANSIENT_AUTH_REPAIR_ERRORS = {"exception", "login_failed", "site_unavailable"}
_TRANSIENT_AUTH_REPAIR_DETAIL_MARKERS = (
    "page.screenshot",
    "timeout",
    "timed out",
    "net::",
    "network",
    "connection",
    "proxy",
    "temporarily unavailable",
    "try again later",
    "context deadline exceeded",
)


def _headers():
    return {"Authorization": f"Bearer {CPA_KEY}"}


def _request(method, path, **kwargs):
    return _CPA_SESSION.request(method, f"{CPA_URL.rstrip('/')}{path}", **kwargs)


def list_cpa_files():
    """获取 CPA 中所有认证文件。远端异常必须显式失败，不能伪装成空列表。"""
    try:
        resp = _request("GET", "/v0/management/auth-files", headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        logger.error("[CPA] 获取文件列表失败: %s", exc)
        raise RuntimeError(f"[CPA] auth-files list request failed: {exc}") from exc

    if resp.status_code != 200:
        logger.error("[CPA] 获取文件列表失败: %d %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"[CPA] auth-files list failed: HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("[CPA] 获取文件列表返回非 JSON 内容: %s", resp.text[:200])
        raise RuntimeError("[CPA] auth-files list returned non-JSON response") from exc

    files = data.get("files", [])
    if not isinstance(files, list):
        raise RuntimeError("[CPA] auth-files list response missing files list")
    return files


def upload_to_cpa(filepath):
    """上传认证文件到 CPA"""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning("[CPA] 文件不存在: %s", filepath)
        return False

    with open(filepath, "rb") as f:
        resp = _request(
            "POST",
            "/v0/management/auth-files",
            headers=_headers(),
            files={"file": (filepath.name, f, "application/json")},
            timeout=10,
        )

    if resp.status_code == 200:
        logger.info("[CPA] 已上传: %s", filepath.name)
        return True
    else:
        logger.error("[CPA] 上传失败: %d %s", resp.status_code, resp.text[:200])
        return False


def delete_from_cpa(name):
    """从 CPA 删除认证文件"""
    resp = _request(
        "DELETE",
        "/v0/management/auth-files",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        logger.info("[CPA] 已删除: %s", name)
        return True
    else:
        logger.error("[CPA] 删除失败: %d %s", resp.status_code, resp.text[:200])
        return False


def download_from_cpa(name):
    """从 CPA 下载认证文件内容。"""
    resp = _request(
        "GET",
        "/v0/management/auth-files/download",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.text
    logger.error("[CPA] 下载失败: %s -> %d %s", name, resp.status_code, resp.text[:200])
    return None


def _parse_expired_timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return time.time() + 3600
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return time.time() + 3600


def _parse_optional_timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _parse_jwt_payload(token):
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _bundle_from_auth_data(auth_data, fallback_name=""):
    id_token = auth_data.get("id_token", "")
    claims = _parse_jwt_payload(id_token) if id_token else {}
    auth_claims = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}

    plan_type = auth_claims.get("chatgpt_plan_type", "")
    if not plan_type and "-team" in fallback_name:
        plan_type = "team"
    if not plan_type and "-plus" in fallback_name:
        plan_type = "plus"
    if not plan_type and "-free" in fallback_name:
        plan_type = "free"
    if not plan_type:
        plan_type = "unknown"

    return {
        "id_token": id_token,
        "access_token": auth_data.get("access_token", ""),
        "refresh_token": auth_data.get("refresh_token", ""),
        "account_id": auth_data.get("account_id", ""),
        "email": auth_data.get("email", ""),
        "plan_type": plan_type,
        "expired": _parse_expired_timestamp(auth_data.get("expired")),
        "last_refresh_ts": _parse_optional_timestamp(auth_data.get("last_refresh")),
    }


def _refresh_account_proxy_url_for_upload(acc: dict, path: Path) -> None:
    email = str(acc.get("email") or "").strip().lower()
    if not email or path.name.startswith("codex-main-"):
        return
    try:
        from autoteam.admin_state import get_admin_email

        if email == (get_admin_email() or "").strip().lower():
            return
    except Exception:
        pass

    required = False
    try:
        from autoteam import config as runtime_config
        from autoteam.ipv6_pool import ipv6_pool

        required = bool(getattr(runtime_config, "AUTOTEAM_IPV6_POOL_REQUIRED", False))
        proxy_url = ipv6_pool.ensure(email) or ""
        if not proxy_url:
            if required:
                raise RuntimeError("IPv6 pool is required but no account proxy was assigned")
            return

        try:
            auth_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            if required:
                raise RuntimeError(f"cannot read auth file for required IPv6 proxy: {path.name}") from exc
            logger.warning("[CPA] 无法读取 auth_file 以刷新 proxy_url，继续原样上传: %s (%s)", path.name, exc)
            return

        if auth_data.get("proxy_url") == proxy_url:
            return
        auth_data["proxy_url"] = proxy_url
        write_text(path, json.dumps(auth_data, indent=2, ensure_ascii=False))
        logger.info("[CPA] 已刷新待同步凭证 proxy_url: %s", email)
    except Exception as exc:
        if required:
            logger.error("[CPA] IPv6 proxy_url 为必需但刷新失败: %s (%s)", email, exc)
            raise
        logger.warning("[CPA] IPv6 proxy_url 刷新失败，继续上传原凭证: %s (%s)", email, exc)


def _has_active_transient_auth_repair_error(acc: dict) -> bool:
    """Avoid mutating CPA while an active account is in auth-repair cooldown."""
    error_type = str(acc.get("auth_last_error") or "").strip()
    detail = str(acc.get("auth_last_error_detail") or "").lower()
    if error_type in _TRANSIENT_AUTH_REPAIR_ERRORS:
        transient = True
    elif error_type:
        transient = False
    else:
        transient = any(marker in detail for marker in _TRANSIENT_AUTH_REPAIR_DETAIL_MARKERS)
    if not transient:
        return False
    try:
        retry_after = float(acc.get("auth_retry_after") or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    return bool(acc.get("auth_retry_paused") or retry_after > time.time())


def _active_auth_publish_decision(acc: dict, path: Path) -> str:
    """Return publish/delete_remote/keep_remote for a local active Codex auth file."""
    if _has_active_transient_auth_repair_error(acc):
        logger.warning(
            "[CPA] active 凭证处于临时 auth_repair 冷却，保留远端副本等待下轮: %s",
            path.name,
        )
        return "keep_remote"

    if not path.exists():
        return "delete_remote"

    try:
        auth_data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[CPA] 跳过无法读取的 active 凭证 %s: %s", path.name, exc)
        return "delete_remote"

    access_token = auth_data.get("access_token")
    if not access_token:
        logger.warning("[CPA] 跳过缺少 access_token 的 active 凭证: %s", path.name)
        return "delete_remote"

    try:
        from autoteam.codex_auth import check_codex_quota

        quota_status, _info = check_codex_quota(
            access_token,
            account_id=acc.get("workspace_account_id") or auth_data.get("account_id"),
        )
    except Exception as exc:
        logger.warning("[CPA] active 凭证实时验证异常，保留远端副本等待下轮: %s (%s)", path.name, exc)
        return "keep_remote"

    if quota_status == "network_error":
        logger.warning("[CPA] active 凭证实时验证网络异常，保留远端副本等待下轮: %s", path.name)
        return "keep_remote"

    if quota_status == "ok":
        try:
            from autoteam.ipv6_pool import ipv6_pool

            email = (acc.get("email") or auth_data.get("email") or "").strip().lower()
            proxy_url = ipv6_pool.ensure(email) or ""
            if proxy_url and auth_data.get("proxy_url") != proxy_url:
                auth_data["proxy_url"] = proxy_url
                write_text(path, json.dumps(auth_data, indent=2, ensure_ascii=False))
                logger.info("[CPA] 已刷新 active 凭证 proxy_url: %s", email)
        except Exception as exc:
            logger.warning("[CPA] active 凭证 IPv6 proxy_url 刷新失败，继续上传原凭证: %s", exc)
        return "publish"

    logger.warning("[CPA] 跳过实时验证失败的 active 凭证: %s (%s)", path.name, quota_status)
    return "delete_remote"


def _normalized_auth_path(bundle, main=False):
    email = bundle.get("email", "")
    account_id = bundle.get("account_id", "")
    if main:
        suffix = account_id or md5(email.encode()).hexdigest()[:8]
        return AUTH_DIR / f"codex-main-{suffix}.json"
    plan_type = bundle.get("plan_type", "unknown")
    hash_id = md5(account_id.encode()).hexdigest()[:8] if account_id else "unknown"
    return AUTH_DIR / f"codex-{email}-{plan_type}-{hash_id}.json"


def _auth_identity(bundle, main=False):
    if main:
        return ("main", bundle.get("account_id") or bundle.get("email") or "")
    return ("codex", (bundle.get("email") or "").lower(), bundle.get("account_id") or "")


def _candidate_score(auth_data, bundle, name, main=False):
    canonical_name = _normalized_auth_path(bundle, main=main).name
    return (
        1 if name == canonical_name else 0,
        bundle.get("last_refresh_ts", _parse_optional_timestamp(auth_data.get("last_refresh"))),
        _parse_expired_timestamp(auth_data.get("expired")),
        len(auth_data.get("refresh_token") or ""),
    )


def _write_auth_file(filepath, bundle):
    ensure_auth_dir()
    auth_data = {
        "type": "codex",
        "id_token": bundle.get("id_token", ""),
        "access_token": bundle.get("access_token", ""),
        "refresh_token": bundle.get("refresh_token", ""),
        "account_id": bundle.get("account_id", ""),
        "email": bundle.get("email", ""),
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("expired", 0))),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("last_refresh_ts", time.time()))),
    }
    write_text(filepath, json.dumps(auth_data, indent=2))
    ensure_auth_file_permissions(filepath)
    return filepath


def _save_normalized_auth_file(bundle, main=False):
    filepath = _normalized_auth_path(bundle, main=main)

    if main:
        for old in AUTH_DIR.glob("codex-main-*.json"):
            if old != filepath and old.exists():
                old.unlink()
    else:
        email = bundle.get("email", "")
        for old in AUTH_DIR.glob(f"codex-{email}-*.json"):
            if old != filepath and old.exists():
                old.unlink()

    return _write_auth_file(filepath, bundle)


def _load_local_best_candidate(identity_key):
    """读取本地同 identity 的最佳候选认证文件。"""
    best = None
    for path in AUTH_DIR.glob("codex-*.json"):
        if not path.is_file():
            continue
        try:
            auth_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if auth_data.get("type") != "codex":
            continue
        main = path.name.startswith("codex-main-")
        bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
        if _auth_identity(bundle, main=main) != identity_key:
            continue
        candidate = {
            "path": path,
            "auth_data": auth_data,
            "bundle": bundle,
            "main": main,
        }
        if best is None or _candidate_score(
            candidate["auth_data"], candidate["bundle"], candidate["path"].name, candidate["main"]
        ) > _candidate_score(best["auth_data"], best["bundle"], best["path"].name, best["main"]):
            best = candidate
    return best


def _cleanup_local_duplicates(accounts=None):
    """清理本地同账号重复认证文件，只保留一个规范文件。"""
    grouped = {}
    for path in AUTH_DIR.glob("codex-*.json"):
        if not path.is_file():
            continue
        try:
            auth_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if auth_data.get("type") != "codex":
            continue
        main = path.name.startswith("codex-main-")
        bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
        key = _auth_identity(bundle, main=main)
        grouped.setdefault(key, []).append(
            {
                "path": path,
                "auth_data": auth_data,
                "bundle": bundle,
                "main": main,
            }
        )

    canonical_map = {}
    removed = 0
    for items in grouped.values():
        if not items:
            continue
        winner = max(
            items, key=lambda item: _candidate_score(item["auth_data"], item["bundle"], item["path"].name, item["main"])
        )
        canonical_path = Path(_save_normalized_auth_file(winner["bundle"], main=winner["main"]))
        canonical_map[_auth_identity(winner["bundle"], main=winner["main"])] = canonical_path
        for item in items:
            if item["path"] != canonical_path and item["path"].exists():
                item["path"].unlink()
                removed += 1

    if accounts is not None:
        changed = False
        for acc in accounts:
            auth_path = acc.get("auth_file")
            if not auth_path:
                continue
            try:
                path = Path(auth_path)
                if not path.exists():
                    continue
                auth_data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
            canonical_path = canonical_map.get(_auth_identity(bundle, main=False))
            if canonical_path and acc.get("auth_file") != str(canonical_path.resolve()):
                acc["auth_file"] = str(canonical_path.resolve())
                changed = True
        return removed, changed

    return removed, False


def sync_from_cpa():
    """
    从 CPA 反向同步认证文件到本地。

    规则：
    - 下载 CPA 中所有 codex 认证文件到本地 auths/
    - 非主号文件会导入/修复到 accounts.json，默认状态为 standby（保守导入）
    - 不删除本地账号记录，仅补充/更新 auth_file
    """
    from autoteam.accounts import (
        STATUS_STANDBY,
        add_account,
        find_account,
        load_accounts,
        save_accounts,
        update_account,
    )
    from autoteam.mail import infer_mail_provider_from_email

    AUTH_DIR.mkdir(exist_ok=True)

    accounts = load_accounts()
    changed_accounts = False
    imported_files = 0
    updated_files = 0
    added_accounts = 0
    updated_accounts = 0
    skipped = 0
    cpa_duplicates_deleted = 0
    local_kept_newer = 0

    local_duplicates_deleted, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    if accounts_path_repaired:
        save_accounts(accounts)

    cpa_files = list_cpa_files()
    if not cpa_files:
        logger.info("[CPA] 未发现可反向同步的认证文件")
        return {
            "downloaded": 0,
            "updated": 0,
            "accounts_added": 0,
            "accounts_updated": 0,
            "skipped": 0,
            "cpa_duplicates_deleted": 0,
            "local_duplicates_deleted": local_duplicates_deleted,
            "local_kept_newer": 0,
            "total": 0,
        }

    candidates = []
    for item in cpa_files:
        name = (item.get("name") or "").strip()
        if not name or not name.endswith(".json") or not name.startswith("codex-"):
            skipped += 1
            continue

        content = download_from_cpa(name)
        if not content:
            skipped += 1
            continue

        try:
            auth_data = json.loads(content)
        except Exception:
            logger.warning("[CPA] 跳过无效 JSON: %s", name)
            skipped += 1
            continue

        if auth_data.get("type") != "codex":
            logger.info("[CPA] 跳过非 codex 文件: %s", name)
            skipped += 1
            continue

        bundle = _bundle_from_auth_data(auth_data, fallback_name=name)
        email = (bundle.get("email") or item.get("email") or "").lower().strip()
        bundle["email"] = email

        if not email and not name.startswith("codex-main-"):
            logger.info("[CPA] 跳过缺少邮箱的文件: %s", name)
            continue

        candidates.append(
            {
                "name": name,
                "auth_data": auth_data,
                "bundle": bundle,
                "main": name.startswith("codex-main-"),
            }
        )

    grouped = {}
    for item in candidates:
        grouped.setdefault(_auth_identity(item["bundle"], main=item["main"]), []).append(item)

    for items in grouped.values():
        winner = max(
            items,
            key=lambda item: _candidate_score(item["auth_data"], item["bundle"], item["name"], main=item["main"]),
        )
        for item in items:
            if item is winner:
                continue
            if delete_from_cpa(item["name"]):
                cpa_duplicates_deleted += 1

        name = winner["name"]
        bundle = winner["bundle"]
        email = bundle.get("email", "")
        identity_key = _auth_identity(bundle, main=winner["main"])
        local_best = _load_local_best_candidate(identity_key)
        cpa_score = _candidate_score(winner["auth_data"], bundle, name, main=winner["main"])
        local_score = None
        if local_best:
            local_score = _candidate_score(
                local_best["auth_data"], local_best["bundle"], local_best["path"].name, main=local_best["main"]
            )

        if winner["main"]:
            if local_best and local_score >= cpa_score:
                local_kept_newer += 1
                normalized_path = local_best["path"]
            else:
                normalized_path = _normalized_auth_path(bundle, main=True)
                existed = normalized_path.exists()
                previous = None
                if existed:
                    try:
                        previous = normalized_path.read_text(encoding="utf-8")
                    except Exception:
                        previous = None

                normalized_path = Path(_save_normalized_auth_file(bundle, main=True))
                current = normalized_path.read_text(encoding="utf-8")
                if not existed:
                    imported_files += 1
                elif previous != current:
                    updated_files += 1
            if normalized_path.name != name:
                old_path = AUTH_DIR / name
                if old_path.exists() and old_path != normalized_path:
                    old_path.unlink()
            continue

        if local_best and local_score >= cpa_score:
            local_kept_newer += 1
            normalized_path = local_best["path"]
        else:
            normalized_path = _normalized_auth_path(bundle)
            existed = normalized_path.exists()
            previous = None
            if existed:
                try:
                    previous = normalized_path.read_text(encoding="utf-8")
                except Exception:
                    previous = None

            normalized_path = Path(_save_normalized_auth_file(bundle))
            current = normalized_path.read_text(encoding="utf-8")

            if not existed:
                imported_files += 1
            elif previous != current:
                updated_files += 1

        acc = find_account(accounts, email)
        resolved_path = str(normalized_path.resolve())
        inferred_provider = infer_mail_provider_from_email(email)
        if acc:
            acc_changed = False
            if acc.get("auth_file") != resolved_path:
                acc["auth_file"] = resolved_path
                acc_changed = True
            if inferred_provider and not acc.get("mail_provider"):
                acc["mail_provider"] = inferred_provider
                acc_changed = True
            if acc_changed:
                changed_accounts = True
                updated_accounts += 1
        else:
            # Round 12 wire-up (M2) — 改用 add_account API 触发 None→PENDING transition,
            # 紧接着 update_account → STANDBY 一次性带上 auth_file. 这样状态机能落每条
            # state_log.jsonl + F2 SSE 推送, 不再静默直 append 旁路.
            try:
                add_account(email, "", cloudmail_account_id=None, mail_provider=inferred_provider or None)
                update_account(
                    email,
                    status=STATUS_STANDBY,
                    auth_file=resolved_path,
                    quota_exhausted_at=None,
                    quota_resets_at=None,
                    _reason="cpa_sync:import_unknown",
                )
                accounts = load_accounts()  # 刷新 in-memory snapshot 给后续 _cleanup_local_duplicates 用
            except Exception as exc:
                logger.warning(
                    "[CPA] 反向同步 add_account/transition 抛异常,回退直 append: %s (%s)",
                    email, exc,
                )
                accounts.append(
                    {
                        "email": email,
                        "password": "",
                        "cloudmail_account_id": None,
                        "mail_provider": inferred_provider or "",
                        "status": STATUS_STANDBY,
                        "auth_file": resolved_path,
                        "quota_exhausted_at": None,
                        "quota_resets_at": None,
                        "created_at": time.time(),
                        "last_active_at": None,
                    }
                )
            changed_accounts = True
            added_accounts += 1

    if changed_accounts:
        save_accounts(accounts)

    local_duplicates_deleted_after, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    local_duplicates_deleted += local_duplicates_deleted_after
    if accounts_path_repaired:
        save_accounts(accounts)

    logger.info(
        "[CPA] 反向同步完成: 新增文件 %d, 更新文件 %d, 新增账号 %d, 更新账号 %d, 保留本地较新 %d, CPA去重 %d, 本地去重 %d, 跳过 %d",
        imported_files,
        updated_files,
        added_accounts,
        updated_accounts,
        local_kept_newer,
        cpa_duplicates_deleted,
        local_duplicates_deleted,
        skipped,
    )
    return {
        "downloaded": imported_files,
        "updated": updated_files,
        "accounts_added": added_accounts,
        "accounts_updated": updated_accounts,
        "skipped": skipped,
        "local_kept_newer": local_kept_newer,
        "cpa_duplicates_deleted": cpa_duplicates_deleted,
        "local_duplicates_deleted": local_duplicates_deleted,
        "total": len(cpa_files),
    }


def sync_to_cpa():
    """
    同步本地认证文件到 CPA。同步范围严格限定为 STATUS_ACTIVE（当前 Team 子号席位）。
    - active 有 auth_file 且实时验证通过 → 上传（覆盖）
    - CPA 有但不在当前 active 集合内 → 从 CPA 删除
    - CPA 中的 codex-main-* 主号凭证会主动删除，避免 CPA 使用母号
    """
    from autoteam.accounts import STATUS_ACTIVE, is_account_disabled, load_accounts, save_accounts

    accounts = load_accounts()
    local_emails = {
        str(a.get("email") or "").strip().lower()
        for a in accounts
        if str(a.get("email") or "").strip()
    }
    local_duplicates_deleted, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    if accounts_path_repaired:
        save_accounts(accounts)

    # 修复断裂的 auth_file 路径
    changed = False
    for acc in accounts:
        auth_path = acc.get("auth_file")
        if auth_path and not Path(auth_path).exists():
            matches = list(AUTH_DIR.glob(f"codex-{acc['email']}-*.json"))
            if matches:
                acc["auth_file"] = str(matches[0].resolve())
                changed = True
    if changed:
        save_accounts(accounts)

    # 需要同步到 CPA 的账号：只允许当前 active Team 子号。
    files_to_sync = {}
    synced_active = 0
    disabled_skipped = 0
    active_publish_skipped = 0
    active_publish_kept_remote = 0
    active_publish_delete_remote = 0
    for acc in accounts:
        if is_account_disabled(acc):
            disabled_skipped += 1
            continue
        status = acc.get("status")
        if status != STATUS_ACTIVE:
            continue
        auth_path = acc.get("auth_file")
        if not auth_path:
            continue
        path = Path(auth_path)
        if not path.exists():
            continue

        decision = _active_auth_publish_decision(acc, path)
        if decision == "keep_remote":
            active_publish_kept_remote += 1
            continue
        if decision == "delete_remote":
            active_publish_delete_remote += 1
            continue
        if decision != "publish":
            active_publish_skipped += 1
            continue

        _refresh_account_proxy_url_for_upload(acc, path)
        files_to_sync[path.name] = path
        synced_active += 1

    # CPA 认证文件
    cpa_files = list_cpa_files()
    cpa_names = {f["name"]: f for f in cpa_files if f.get("name")}
    min_active_for_remote_delete = max(1, int(AUTO_CHECK_TARGET_SEATS) - 1)
    allow_remote_delete = synced_active >= min_active_for_remote_delete

    logger.info(
        "[CPA] 待同步 active 子号认证文件: %d (Team=%d), CPA 现有: %d",
        len(files_to_sync),
        synced_active,
        len(cpa_files),
    )
    if not allow_remote_delete:
        logger.warning(
            "[CPA] active 凭证不足，跳过本轮远端删除: %d/%d",
            synced_active,
            min_active_for_remote_delete,
        )

    # 上传：当前 active 子号认证文件（覆盖同名文件，确保 token 最新）
    uploaded = 0
    for name, path in files_to_sync.items():
        logger.info("[CPA] 上传: %s", name)
        if upload_to_cpa(path):
            uploaded += 1

    deleted = 0
    skipped_remote_delete = 0
    for name, cpa_file in cpa_names.items():
        email = cpa_file.get("email", "").lower()
        managed_codex = (
            name.startswith("codex-main-")
            or name in files_to_sync
            or email in local_emails
            or name.startswith("codex-")
        )
        if managed_codex and name not in files_to_sync:
            if not allow_remote_delete:
                skipped_remote_delete += 1
                continue
            logger.info("[CPA] 删除非当前 active 子号文件: %s (%s)", name, email)
            if delete_from_cpa(name):
                deleted += 1
    if skipped_remote_delete:
        logger.warning("[CPA] 本轮因 active 凭证不足保留远端非 active 文件: %d", skipped_remote_delete)

    if disabled_skipped:
        logger.info("[CPA] 跳过 %d 个本地禁用账号", disabled_skipped)

    logger.info("[CPA] 同步完成: 上传 %d, 删除 %d, 本地去重 %d", uploaded, deleted, local_duplicates_deleted)

    # 最终状态
    final_cpa = list_cpa_files()
    final_local_managed = [f for f in final_cpa if f.get("email", "").lower() in local_emails]
    logger.info(
        "[CPA] CPA 中本地管理: %d, 本地待同步 active: %d",
        len(final_local_managed),
        len(files_to_sync),
    )
    return {
        "ok": True,
        "uploaded": uploaded,
        "deleted": deleted,
        "remote_count_before": len(cpa_files),
        "remote_managed_after": len(final_local_managed),
        "synced_active": synced_active,
        "synced_personal": 0,
        "disabled_skipped": disabled_skipped,
        "active_publish": {
            "skipped_unknown": active_publish_skipped,
            "kept_remote": active_publish_kept_remote,
            "delete_remote": active_publish_delete_remote,
        },
        "local_duplicates_deleted": local_duplicates_deleted,
        "delete_guard": {
            "allow_remote_delete": allow_remote_delete,
            "min_active_for_remote_delete": min_active_for_remote_delete,
            "skipped_remote_delete": skipped_remote_delete,
            "skipped_protected": 0,
            "skipped_protected_delete": 0,
        },
    }


def sync_main_codex_to_cpa(filepath):
    """同步主号 Codex 认证文件到 CPA。"""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"主号认证文件不存在: {filepath}")

    name = filepath.name
    existing = {item.get("name"): item for item in list_cpa_files()}

    for old_name in existing:
        if old_name and old_name.startswith("codex-main-"):
            logger.info("[CPA] 删除旧主号文件: %s", old_name)
            delete_from_cpa(old_name)

    if not upload_to_cpa(filepath):
        raise RuntimeError(f"上传主号认证文件失败: {name}")

    # Round 12 S7 — 主号同步成功后,记录当前 active workspace,便于上游观测
    try:
        active = _get_active_workspace_summary()
        if active:
            logger.info(
                "[CPA] 主号 Codex 已同步 (active workspace: id=%s admin=%s account_id=%s)",
                active.get("id"), active.get("admin_email"), active.get("account_id"),
            )
        else:
            logger.info("[CPA] 主号 Codex 已同步: %s", name)
    except Exception:
        logger.info("[CPA] 主号 Codex 已同步: %s", name)
    return {"uploaded": name}


def _get_active_workspace_summary():
    """Round 12 S7 — return the current active workspace (id/admin/account_id) or None.

    Best-effort: never raises; pool unavailable returns None.
    """
    try:
        from autoteam.workspace_pool import default_pool

        active = default_pool.get_active()
        if not active:
            return None
        return {
            "id": active.get("id"),
            "admin_email": active.get("admin_email"),
            "account_id": active.get("account_id"),
        }
    except Exception:
        return None


def get_active_sync_target():
    """Round 12 S7 — public helper: which workspace identity should CPA sync to?

    Returns dict({id, admin_email, account_id}) or None when single-workspace
    mode (caller falls back to legacy admin_state).
    """
    return _get_active_workspace_summary()
