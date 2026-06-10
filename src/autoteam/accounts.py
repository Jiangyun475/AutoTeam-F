"""账号池管理 - 持久化存储所有账号状态"""

import json
import logging
import threading
import time
from pathlib import Path

from autoteam.account_state import (
    AccountState,
    IllegalTransitionError,
    default_machine,
)
from autoteam.admin_state import get_admin_email
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
ACCOUNTS_FILE = PROJECT_ROOT / "accounts.json"

# Round 12 S6 — 并发 cmd_rotate 引入: 包裹 load → mutate → save 的 RMW 段,
# 避免两个 worker thread 各读一份后互相覆盖. update_account / delete_account /
# add_account 调用方都已通过此锁串行,纯只读的 load_accounts/find_account
# 仍然 lock-free(append-only JSONL state_log 的 atomic write 在状态机内).
_accounts_io_lock = threading.RLock()

# 账号状态
STATUS_ACTIVE = "active"  # 在 team 中，额度可用
STATUS_EXHAUSTED = "exhausted"  # 在 team 中，额度用完
STATUS_STANDBY = "standby"  # 已移出 team，等待额度恢复
STATUS_PENDING = "pending"  # 已邀请，等待注册完成
STATUS_PERSONAL = "personal"  # 已主动退出 team，走个人号 Codex OAuth，不再参与 Team 轮转
STATUS_AUTH_INVALID = "auth_invalid"  # auth_file token 已不可用(401/403),待 reconcile 清理或重登
STATUS_AUTH_PENDING = STATUS_AUTH_INVALID  # 向后兼容 target 命名，落盘值仍为 auth_invalid
STATUS_ORPHAN = "orphan"  # 在 workspace 里占着席位,但本地没 auth_file(残废,待人工介入或兜底 kick)
# Round 9 SPEC v2.0 — 母号 cancel_at_period_end 期内子号过渡态:
# 仍持有有效 auth_file + token,wham 200 plan=team 可继续消耗配额;
# 不参与 fill / cmd_rotate / cmd_check 工作池,grace_until 到期才转 STANDBY。
# 仅由 _apply_master_degraded_classification helper 写入与撤回(I10 不变量)。
STATUS_DEGRADED_GRACE = "degraded_grace"

# 席位类型:标记该账号在 ChatGPT Team 里被授予的席位种类,用于下游 fill / check 区分对待
SEAT_CHATGPT = "chatgpt"  # 完整 ChatGPT 席位(PATCH invite seat_type=default 成功)
SEAT_CODEX = "codex"  # 仅 Codex 席位(usage_based,PATCH 改 default 失败时保留的兜底)
SEAT_UNKNOWN = "unknown"  # 未知/未记录,老账号或手动导入默认值

# SPEC-2 §shared/plan-type-whitelist:本系统能正确处理(注册→入池→Codex 调用)的 plan_type 集合。
# 不在此集合内的字面量(self_serve_business_usage_based / enterprise / unknown 等)
# 均视为 unsupported,触发 STATUS_AUTH_INVALID + register_failures.category="plan_unsupported"。
# 修改本集合需经测试验证(quota / seat 行为有变化)。
SUPPORTED_PLAN_TYPES = frozenset({
    "team",   # ChatGPT Team workspace,本系统主要工作池
    "free",   # 已退出 Team 的个人 free,personal 子号路径
    "plus",   # 个人付费,允许通过 manual_account 手动添加
    "pro",    # 个人 Pro,同上
})


def normalize_plan_type(plan_type):
    """归一化用于落盘 / 比对的 plan_type。

    None / 空串 → "unknown",其余统一 .lower().strip()。
    比对前先归一化,避免 OpenAI 后端大小写漂移(返回 "Team" / "Self_Serve_*")。
    """
    if not plan_type:
        return "unknown"
    return str(plan_type).strip().lower()


def is_supported_plan(plan_type):
    """判定 plan_type 是否在白名单内。"""
    if not plan_type:
        return False
    return normalize_plan_type(plan_type) in SUPPORTED_PLAN_TYPES


def _normalized_email(value):
    return (value or "").strip().lower()


def _is_main_account_email(email):
    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def is_account_disabled(acc: dict | None) -> bool:
    """Return whether an account is locally disabled for automation flows."""
    return bool((acc or {}).get("disabled", False))


def _normalize_account(acc: dict) -> dict:
    normalized = dict(acc or {})
    normalized["disabled"] = bool(normalized.get("disabled", False))
    normalized["mail_provider"] = str(normalized.get("mail_provider") or "").strip().lower()
    mail_account_id = normalized.get("mail_account_id")
    normalized["mail_account_id"] = (
        str(mail_account_id).strip() if mail_account_id is not None and str(mail_account_id).strip() else None
    )
    service_id = normalized.get("mail_service_id")
    normalized["mail_service_id"] = (
        str(service_id).strip() if service_id is not None and str(service_id).strip() else None
    )
    return normalized


def load_accounts():
    """加载账号列表"""
    if ACCOUNTS_FILE.exists():
        text = read_text(ACCOUNTS_FILE).strip()
        if text:
            data = json.loads(text)
            if isinstance(data, list):
                return [_normalize_account(acc) for acc in data if isinstance(acc, dict)]
    return []


def save_accounts(accounts):
    """保存账号列表"""
    normalized = [_normalize_account(acc) for acc in accounts if isinstance(acc, dict)]
    write_text(ACCOUNTS_FILE, json.dumps(normalized, indent=2, ensure_ascii=False))


def find_account(accounts, email):
    """按邮箱查找账号"""
    target = _normalized_email(email)
    for acc in accounts:
        if _normalized_email(acc.get("email")) == target:
            return acc
    return None


def add_account(
    email,
    password,
    cloudmail_account_id=None,
    seat_type=SEAT_UNKNOWN,
    workspace_account_id=None,
    *,
    mail_provider=None,
    mail_account_id=None,
    mail_service_id=None,
):
    """添加新账号。

    seat_type 取值见 SEAT_CHATGPT / SEAT_CODEX / SEAT_UNKNOWN。
    workspace_account_id:邀请该号时所属的母号 workspace account_id(ChatGPT Team
    workspace 唯一 ID)。母号切换后,记录的 workspace_account_id 与当前 workspace
    不一致 → sync_account_states 不会把这种"前母号留下来的号"误打成 standby。
    新号不指定时为 None,旧记录走兼容回退。
    """
    accounts = load_accounts()
    existing = find_account(accounts, email)
    if existing:
        # 已存在仍允许补写 seat_type / workspace_account_id,避免旧记录一直缺字段
        patch = {}
        if seat_type and seat_type != SEAT_UNKNOWN:
            patch["seat_type"] = seat_type
        if workspace_account_id and not existing.get("workspace_account_id"):
            patch["workspace_account_id"] = workspace_account_id
        if mail_provider and not existing.get("mail_provider"):
            patch["mail_provider"] = str(mail_provider).strip().lower()
        if mail_account_id is not None and not existing.get("mail_account_id"):
            patch["mail_account_id"] = str(mail_account_id).strip()
        if mail_service_id and not existing.get("mail_service_id"):
            patch["mail_service_id"] = str(mail_service_id).strip()
        if patch:
            update_account(email, **patch)
        return

    with _accounts_io_lock:
        # 二次 load,避免在拿锁前已被其他 worker 追加
        accounts = load_accounts()
        if find_account(accounts, email):
            return
        accounts.append(
            {
                "email": email,
                "password": password,
                "cloudmail_account_id": cloudmail_account_id,
                "mail_provider": str(mail_provider or "").strip().lower(),
                "mail_account_id": (
                    str(mail_account_id).strip()
                    if mail_account_id is not None and str(mail_account_id).strip()
                    else None
                ),
                "mail_service_id": (
                    str(mail_service_id).strip()
                    if mail_service_id is not None and str(mail_service_id).strip()
                    else None
                ),
                "status": STATUS_PENDING,
                "seat_type": seat_type or SEAT_UNKNOWN,
                "workspace_account_id": workspace_account_id,  # 邀请时所在的母号 workspace ID,母号切换检测用
                # Round 11 V8 — 子号自身的 personal workspace UUID(POST /backend-api/accounts/personal idempotent
                # getOrCreate),持久化后下次 OAuth 不必再 fetch;失败/旧记录为 None,运行时按需 fetch + 回填。
                "personal_workspace_id": None,
                "auth_file": None,  # CPA 认证文件路径
                "quota_exhausted_at": None,  # 额度用完的时间
                "quota_resets_at": None,  # 额度恢复时间
                "last_quota_check_at": None,  # 最近一次 wham/usage 探测时间戳,用于 standby 探测去重
                "disabled": False,  # 本地禁用:保留记录和 auth_file,但自动化巡检/轮转/同步跳过
                # Round 11 V7 — 双失效探测(access_token + refresh_token 同时被 server-side invalidate):
                # 主循环周期性调 is_token_pair_invalidated,命中后落该字段供事后排查 / UI 展示。
                "last_token_pair_invalidated_at": None,
                "created_at": time.time(),
                "last_active_at": None,
            }
        )
        save_accounts(accounts)
    # Round 12 S1 — 新号入池触发 None → PENDING 转移,产生事件 + state_log 行
    try:
        default_machine.transition(
            email=email,
            to_state=STATUS_PENDING,
            reason="add_account",
            from_state=None,
        )
    except IllegalTransitionError:
        logger.exception("add_account: illegal initial transition for %s", email)


def update_account(email, **kwargs):
    """更新账号字段。

    如果 kwargs 含 ``status`` 且与当前状态不同 → 走 ``default_machine.transition``
    做合法性校验 + 写 state_log + 发事件。其余字段照常 update。

    可选 kwarg ``_reason`` (内部约定): 状态转移日志里的 reason,默认 "update_account"。

    Round 12 S6 — RMW 段(load → mutate → save)由 ``_accounts_io_lock`` 串行,
    并发 cmd_rotate worker 不会互相覆盖. state_machine.transition 内部已有
    自己的锁负责 state_log atomic write,本锁仅保证 accounts.json 一致性.
    """
    with _accounts_io_lock:
        accounts = load_accounts()
        acc = find_account(accounts, email)
        if not acc:
            return None

        new_status = kwargs.get("status")
        cur_status = acc.get("status")
        reason = kwargs.pop("_reason", "update_account")

        if new_status is not None and new_status != cur_status:
            extra_payload = {k: v for k, v in kwargs.items() if k != "status"}
            try:
                default_machine.transition(
                    email=email,
                    to_state=new_status,
                    reason=reason,
                    extra=extra_payload or None,
                    from_state=cur_status,
                )
            except IllegalTransitionError:
                # 合法性校验失败一律抛给调用方,避免静默写坏 accounts.json
                raise

        acc.update(kwargs)
        save_accounts(accounts)
        return acc


def delete_account(email):
    """从账号池彻底移除（不动认证文件、不动 CloudMail 邮箱）。返回是否真的删除了记录。"""
    with _accounts_io_lock:
        accounts = load_accounts()
        remaining = [a for a in accounts if a.get("email") != email]
        if len(remaining) == len(accounts):
            return False
        save_accounts(remaining)
        return True


def get_active_accounts():
    """获取所有活跃账号"""
    return [
        a
        for a in load_accounts()
        if a["status"] == STATUS_ACTIVE
        and not _is_main_account_email(a.get("email"))
        and not is_account_disabled(a)
    ]


def get_personal_accounts():
    """获取所有已退出 Team、走个人 Codex 授权的账号（不参与席位轮转）"""
    return [a for a in load_accounts() if a["status"] == STATUS_PERSONAL and not _is_main_account_email(a.get("email"))]


def _quota_remaining_pct(acc: dict, key: str) -> int | None:
    quota = acc.get("last_quota") if isinstance(acc.get("last_quota"), dict) else {}
    value = quota.get(key)
    if not isinstance(value, (int, float)):
        return None
    return max(0, min(100, 100 - int(value)))


def _standby_sort_key(acc: dict) -> tuple:
    recovered = bool(acc.get("_quota_recovered", False))
    resets_at = acc.get("quota_resets_at")
    if not isinstance(resets_at, (int, float)) or resets_at <= 0:
        resets_at = 0
    exhausted_at = acc.get("quota_exhausted_at")
    if not isinstance(exhausted_at, (int, float)) or exhausted_at <= 0:
        exhausted_at = 0

    primary_remaining = _quota_remaining_pct(acc, "primary_pct")
    weekly_remaining = _quota_remaining_pct(acc, "weekly_pct")

    # 先保证 5h 已恢复的号排前；同一组内优先使用周额度更充足、5h 更充足的号。
    # 缺少额度快照的旧记录保留可用性，但排在有明确快照的候选之后。
    return (
        not recovered,
        0 if recovered else float(resets_at or 9999999999),
        weekly_remaining is None,
        -(weekly_remaining if weekly_remaining is not None else 0),
        primary_remaining is None,
        -(primary_remaining if primary_remaining is not None else 0),
        float(exhausted_at),
        str(acc.get("email") or ""),
    )


def get_standby_accounts():
    """获取所有待命账号（已移出 team，可能额度已恢复）"""
    accounts = load_accounts()
    now = time.time()
    standby = []
    for a in accounts:
        if _is_main_account_email(a.get("email")):
            continue
        if is_account_disabled(a):
            continue
        if a["status"] == STATUS_STANDBY:
            resets_at = a.get("quota_resets_at")
            if resets_at is None:
                # 没有恢复时间 = 不是因为额度用完被移出的，随时可复用
                a["_quota_recovered"] = True
            else:
                # 有恢复时间，看是否已过
                a["_quota_recovered"] = now >= resets_at
            standby.append(a)
    # 已恢复的排前面；同为已恢复时优先周额度/5h 额度更健康的账号。
    standby.sort(key=_standby_sort_key)
    return standby


def get_next_reusable_account():
    """获取下一个可重用的 standby 账号（优先额度已恢复的）"""
    standby = get_standby_accounts()
    if standby:
        return standby[0]
    return None


# ---------------------------------------------------------------------------
# Round 12 S1 — 把 default_machine 的 from_state 查询挂在 accounts.json。
# 放在文件尾部避免循环 import：account_state.py 加载时不会触发本模块,本模块
# 加载完成后再注入 provider。
# ---------------------------------------------------------------------------
def _lookup_account_status(email: str):
    """状态机的 state_provider：按 email 查 accounts.json 当前 status。"""
    if not email:
        return None
    acc = find_account(load_accounts(), email)
    if not acc:
        return None
    return acc.get("status")


default_machine.set_state_provider(_lookup_account_status)


# 显式 re-export AccountState：调用方 `from autoteam.accounts import AccountState` 也能拿到。
# `IllegalTransitionError` 同理供上层捕获。
__all__ = [
    "ACCOUNTS_FILE",
    "AccountState",
    "IllegalTransitionError",
    "PROJECT_ROOT",
    "SEAT_CHATGPT",
    "SEAT_CODEX",
    "SEAT_UNKNOWN",
    "STATUS_ACTIVE",
    "STATUS_AUTH_INVALID",
    "STATUS_DEGRADED_GRACE",
    "STATUS_EXHAUSTED",
    "STATUS_ORPHAN",
    "STATUS_PENDING",
    "STATUS_PERSONAL",
    "STATUS_STANDBY",
    "SUPPORTED_PLAN_TYPES",
    "add_account",
    "delete_account",
    "find_account",
    "get_active_accounts",
    "get_next_reusable_account",
    "get_personal_accounts",
    "get_standby_accounts",
    "is_account_disabled",
    "is_supported_plan",
    "load_accounts",
    "normalize_plan_type",
    "save_accounts",
    "update_account",
]
