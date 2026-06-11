"""Round 12 S3 — cherry-pick from upstream team rotate functions.

Verifies the ✓ paste / ⚠ adapt items from the S0 diff report
(`.trellis/tasks/05-11-s0-upstream-team-rotate-diff/research/upstream-diff.md`):

1. invite_to_team module-level helper (verbatim paste)
2. signup_profile.py module + register_with_invite signup_profile= passthrough
3. Pool counting helpers (_pool_active_target / _count_pool_active_accounts /
   _count_local_team_seat_accounts / _estimate_local_team_member_count)
4. _release_auth_repair_team_seat
5. _record_auth_repair_failure (3 branches: add_phone soft retry / hard pause / decay)
6. _auth_repair_skip_reason / _auth_repair_state_suffix / _auth_repair_reset
7. _login_codex_with_result result-shape and retry guard

All status writes route through update_account → default_machine.transition,
so we mock that boundary, not OpenAI APIs.
"""
from __future__ import annotations

import random
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autoteam import accounts as accounts_mod
from autoteam import manager as manager_mod
from autoteam.account_state import default_machine
from autoteam.signup_profile import (
    MAX_SIGNUP_AGE,
    MIN_SIGNUP_AGE,
    SignupProfile,
    calculate_age,
    generate_signup_profile,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_accounts(tmp_path: Path, monkeypatch):
    """Isolate accounts.json + default_machine log + admin email to tmp."""
    accounts_file = tmp_path / "accounts.json"
    log_file = tmp_path / "state_log.jsonl"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts_mod, "get_admin_email", lambda: "")
    # patch get_admin_email in manager too (used by _is_main_account_email)
    monkeypatch.setattr(manager_mod, "get_admin_email", lambda: "")
    original_log = default_machine._log_path
    default_machine._log_path = log_file
    yield accounts_file
    default_machine._log_path = original_log


# ===========================================================================
# 1. SignupProfile + generate_signup_profile
# ===========================================================================
class TestSignupProfile:
    def test_generate_signup_profile_yields_consistent_snapshot(self):
        profile = generate_signup_profile()
        assert isinstance(profile, SignupProfile)
        # full_name always two words ("First Last")
        assert " " in profile.full_name and len(profile.full_name.split()) == 2
        # birthday is dict with year / month / day strings
        for key in ("year", "month", "day"):
            assert key in profile.birthday and profile.birthday[key]
        # age is non-empty string
        assert profile.age and isinstance(profile.age, str)

    def test_generate_signup_profile_derives_age_from_birthday(self):
        today = date(2026, 5, 15)
        profile = generate_signup_profile(today=today, rng=random.Random(7))

        assert profile.age == str(calculate_age(profile.birth_date, today))
        assert MIN_SIGNUP_AGE <= int(profile.age) <= MAX_SIGNUP_AGE

    def test_birthday_text_format_for_logs(self):
        profile = SignupProfile(
            full_name="Test User",
            birthday={"year": "1990", "month": "06", "day": "15"},
            age="35",
        )
        assert profile.birthday_text == "1990-06-15"
        assert profile.age_text == "35"

    def test_positional_birthday_orders_returns_y_m_d(self):
        profile = SignupProfile(
            full_name="x",
            birthday={"year": "2000", "month": "01", "day": "02"},
            age="25",
        )
        orders = profile.positional_birthday_orders()
        assert orders == [
            ["2000", "01", "02"],
            ["01", "02", "2000"],
            ["02", "01", "2000"],
        ]

    def test_generate_signup_profile_respects_injected_rng(self):
        today = date(2026, 5, 15)

        first = generate_signup_profile(today=today, rng=random.Random(1234))
        second = generate_signup_profile(today=today, rng=random.Random(1234))

        assert first == second

    def test_signup_profile_is_immutable(self):
        profile = generate_signup_profile()
        with pytest.raises(AttributeError):
            profile.full_name = "Mutated"  # type: ignore[misc]
        with pytest.raises(TypeError):
            profile.birthday["year"] = "1900"
        with pytest.raises(TypeError):
            profile.birthday.update({"month": "01"})

    def test_signup_profile_copies_birthday_input(self):
        birthday = {"year": "1990", "month": "06", "day": "15"}
        profile = SignupProfile(full_name="Test User", birthday=birthday, age="35")

        birthday["year"] = "1900"

        assert profile.birthday["year"] == "1990"

    def test_signup_profile_is_hashable_like_mother_template(self):
        today = date(2026, 5, 15)
        rng = random.Random(99)

        seen = {generate_signup_profile(today=today, rng=rng) for _ in range(12)}

        assert len(seen) > 1


# ===========================================================================
# 2. register_with_invite signup_profile passthrough
# ===========================================================================
class TestRegisterWithInviteSignupProfile:
    def test_register_with_invite_accepts_signup_profile_kwarg(self):
        """signup_profile is a backward-compatible optional kwarg."""
        import inspect

        from autoteam.invite import register_with_invite

        sig = inspect.signature(register_with_invite)
        assert "signup_profile" in sig.parameters
        assert sig.parameters["signup_profile"].default is None


# ===========================================================================
# 3. Pool counting helpers
# ===========================================================================
class TestPoolCountingHelpers:
    def test_pool_active_target_subtracts_main_seat(self):
        # target=3 → 子号 active 目标 = 2 (主号占 1 席)
        assert manager_mod._pool_active_target(3) == 2
        assert manager_mod._pool_active_target(manager_mod._clamp_team_target_seats(5)) == 2
        assert manager_mod._pool_active_target(1) == 0
        assert manager_mod._pool_active_target(0) == 0
        # negative defensiveness
        assert manager_mod._pool_active_target(-3) == 0

    def test_count_pool_active_accounts_filters_main_and_non_active(self):
        accounts = [
            {"email": "a@x.com", "status": "active"},
            {"email": "b@x.com", "status": "active"},
            {"email": "c@x.com", "status": "exhausted"},
            {"email": "d@x.com", "status": "standby"},
            {"email": "e@x.com", "status": "auth_invalid"},
        ]
        assert manager_mod._count_pool_active_accounts(accounts) == 2

    def test_count_pool_active_accounts_require_auth_filters_no_auth_file(
        self, tmp_path: Path
    ):
        existing = tmp_path / "auth.json"
        existing.write_text("{}", encoding="utf-8")
        accounts = [
            {"email": "a@x.com", "status": "active", "auth_file": str(existing)},
            {"email": "b@x.com", "status": "active", "auth_file": ""},
            {"email": "c@x.com", "status": "active"},  # no auth_file key
        ]
        # plain count: 3
        assert manager_mod._count_pool_active_accounts(accounts) == 3
        # require_auth: only the one with valid file
        assert manager_mod._count_pool_active_accounts(accounts, require_auth=True) == 1

    def test_count_local_team_seat_accounts_includes_three_seat_states(self):
        accounts = [
            {"email": "a@x.com", "status": "active"},
            {"email": "b@x.com", "status": "exhausted"},
            {"email": "c@x.com", "status": "auth_invalid"},
            {"email": "d@x.com", "status": "standby"},  # 不占 Team 席位
            {"email": "e@x.com", "status": "personal"},  # 已退 Team
        ]
        assert manager_mod._count_local_team_seat_accounts(accounts) == 3

    def test_estimate_local_team_member_count_adds_main_seat(self):
        accounts = [
            {"email": "a@x.com", "status": "active"},
            {"email": "b@x.com", "status": "exhausted"},
        ]
        # target=3 → reserved_main=1 → 2 + 1 = 3
        assert manager_mod._estimate_local_team_member_count(3, accounts) == 3
        # target=0 → reserved_main=0 → 2 + 0 = 2
        assert manager_mod._estimate_local_team_member_count(0, accounts) == 2


# ===========================================================================
# 4. _has_auth_file
# ===========================================================================
class TestHasAuthFile:
    def test_has_auth_file_returns_true_for_existing_file(self, tmp_path: Path):
        f = tmp_path / "auth.json"
        f.write_text("{}", encoding="utf-8")
        assert manager_mod._has_auth_file({"auth_file": str(f)}) is True

    def test_has_auth_file_false_for_missing(self):
        assert manager_mod._has_auth_file({"auth_file": "/nonexistent.json"}) is False
        assert manager_mod._has_auth_file({"auth_file": ""}) is False
        assert manager_mod._has_auth_file({}) is False
        assert manager_mod._has_auth_file(None) is False


# ===========================================================================
# 5. _chatgpt_session_ready
# ===========================================================================
class TestChatgptSessionReady:
    def test_none_returns_false(self):
        assert manager_mod._chatgpt_session_ready(None) is False

    def test_with_browser_returns_true(self):
        api = MagicMock()
        api.browser = MagicMock()
        # No is_started method
        del api.is_started
        assert manager_mod._chatgpt_session_ready(api) is True

    def test_no_browser_returns_false(self):
        api = MagicMock()
        api.browser = None
        api.http_transport = None
        del api.is_started
        assert manager_mod._chatgpt_session_ready(api) is False

    def test_http_transport_fallback_returns_true(self):
        api = MagicMock()
        api.browser = None
        api.http_transport = MagicMock()
        del api.is_started
        assert manager_mod._chatgpt_session_ready(api) is True

    def test_is_started_takes_precedence(self):
        api = MagicMock()
        api.browser = None
        api.is_started = MagicMock(return_value=True)
        assert manager_mod._chatgpt_session_ready(api) is True


# ===========================================================================
# 6. invite_to_team helper
# ===========================================================================
class TestInviteToTeam:
    def test_returns_true_on_200_no_errored(self, monkeypatch):
        api = MagicMock()
        api.is_started = MagicMock(return_value=True)
        api.invite_member.return_value = (200, {"errored_emails": []})
        monkeypatch.setattr(manager_mod, "_prepare_remote_capacity_for_new_seat", lambda *args, **kwargs: True)
        assert manager_mod.invite_to_team(api, "x@example.com") is True
        api.invite_member.assert_called_once_with("x@example.com", seat_type="default")

    def test_falls_back_to_usage_based_when_default_errored(self, monkeypatch):
        api = MagicMock()
        api.is_started = MagicMock(return_value=True)
        api.invite_member.side_effect = [
            (200, {"errored_emails": [{"error": "default_blocked"}]}),
            (200, {"errored_emails": []}),
        ]
        monkeypatch.setattr(manager_mod, "_prepare_remote_capacity_for_new_seat", lambda *args, **kwargs: True)
        assert manager_mod.invite_to_team(api, "x@example.com") is True
        assert api.invite_member.call_count == 2
        assert api.invite_member.call_args_list[1][1] == {"seat_type": "usage_based"}

    def test_returns_false_when_usage_based_also_errored(self):
        api = MagicMock()
        api.invite_member.side_effect = [
            (200, {"errored_emails": [{"error": "x"}]}),
            (200, {"errored_emails": [{"error": "y"}]}),
        ]
        assert manager_mod.invite_to_team(api, "x@example.com") is False

    def test_returns_false_on_non_200(self):
        api = MagicMock()
        api.invite_member.return_value = (500, {"_raw": "error"})
        assert manager_mod.invite_to_team(api, "x@example.com") is False


# ===========================================================================
# 7. auth_repair helpers
# ===========================================================================
class TestAuthRepairHelpers:
    def test_reset_fields_returns_six_keys(self):
        fields = manager_mod._auth_repair_reset_fields()
        assert set(fields.keys()) == {
            "auth_retry_count",
            "auth_last_error",
            "auth_last_error_detail",
            "auth_last_failed_at",
            "auth_retry_after",
            "auth_retry_paused",
        }
        assert fields["auth_retry_count"] == 0
        assert fields["auth_retry_paused"] is False

    def test_retry_delays_are_2_4_6x_interval(self, monkeypatch):
        # Stub the config import inside the helper (it imports each call)
        import autoteam.config as cfg

        monkeypatch.setattr(cfg, "AUTO_CHECK_INTERVAL", 300)
        # _auto_check_config may override; stub the api module too if importable
        try:
            import autoteam.api as api_mod

            monkeypatch.setattr(api_mod, "_auto_check_config", {}, raising=False)
        except Exception:
            pass
        delays = manager_mod._auth_repair_retry_delays()
        assert delays == (600, 1200, 1800)

    def test_add_phone_retry_delays_are_exponential(self, monkeypatch):
        import autoteam.config as cfg

        monkeypatch.setattr(cfg, "AUTO_CHECK_INTERVAL", 60)
        try:
            import autoteam.api as api_mod

            monkeypatch.setattr(api_mod, "_auto_check_config", {}, raising=False)
        except Exception:
            pass
        delays = manager_mod._auth_repair_add_phone_retry_delays(max_retries=4)
        # max(60, 60) = 60 → (60, 120, 240, 480)
        assert delays == (60, 120, 240, 480)

    def test_error_label_maps_known_types(self):
        assert manager_mod._auth_repair_error_label("add_phone") == "手机号验证"
        assert manager_mod._auth_repair_error_label("human_verification") == "人机验证"
        assert manager_mod._auth_repair_error_label("oauth_timeout") == "OAuth 授权页超时"
        assert manager_mod._auth_repair_error_label("unsupported_region") == "出口地区不被 OAuth 接受"
        assert manager_mod._oauth_retry_delay_seconds("oauth_timeout") == 8
        assert manager_mod._oauth_retry_delay_seconds("account_selection") == 6
        assert manager_mod._oauth_retry_delay_seconds("custom_x") == 0
        # Unknown returns the input string as-is
        assert manager_mod._auth_repair_error_label("custom_x") == "custom_x"
        assert manager_mod._auth_repair_error_label(None) == "未知错误"

    def test_state_suffix_paused(self):
        suffix = manager_mod._auth_repair_state_suffix({"auth_retry_paused": True})
        assert "已暂停" in suffix

    def test_state_suffix_with_retry_after(self):
        import time

        future = time.time() + 600  # 10 minutes
        suffix = manager_mod._auth_repair_state_suffix({"auth_retry_after": future})
        assert "分钟后重试" in suffix

    def test_state_suffix_empty_for_no_state(self):
        assert manager_mod._auth_repair_state_suffix(None) == ""
        assert manager_mod._auth_repair_state_suffix({}) == ""


class TestAuthRepairSkipReason:
    def test_force_overrides_paused(self):
        acc = {"auth_retry_paused": True, "auth_last_error": "human_verification"}
        assert manager_mod._auth_repair_skip_reason(acc, force=True) is None

    def test_paused_returns_chinese_reason(self):
        acc = {"auth_retry_paused": True, "auth_last_error": "human_verification"}
        reason = manager_mod._auth_repair_skip_reason(acc)
        assert reason and "已暂停" in reason and "人机验证" in reason

    def test_within_cooldown_returns_reason(self):
        import time

        future = time.time() + 1200
        acc = {"auth_retry_after": future, "auth_last_error": "add_phone"}
        reason = manager_mod._auth_repair_skip_reason(acc)
        assert reason and "冷却" in reason and "手机号验证" in reason

    def test_past_cooldown_returns_none(self):
        import time

        past = time.time() - 100
        acc = {"auth_retry_after": past, "auth_last_error": "add_phone"}
        assert manager_mod._auth_repair_skip_reason(acc) is None

    def test_none_acc_returns_none(self):
        assert manager_mod._auth_repair_skip_reason(None) is None


class TestLoginCodexWithResult:
    def test_retries_retryable_failures_within_same_round(self, monkeypatch):
        attempts = {"count": 0}

        def fake_login(email, password, mail_client=None, return_result=False):
            assert email == "user@example.com"
            assert password == ""
            assert mail_client is None
            assert return_result is True
            attempts["count"] += 1
            if attempts["count"] < 3:
                return {
                    "ok": False,
                    "bundle": None,
                    "error_type": "auth_code_missing",
                    "error_detail": "未获取到 auth code",
                    "retryable": True,
                }
            return {
                "ok": True,
                "bundle": {"email": email, "plan_type": "team"},
                "error_type": None,
                "error_detail": None,
                "retryable": False,
            }

        monkeypatch.setattr(manager_mod, "login_codex_via_browser", fake_login)

        result = manager_mod._login_codex_with_result("user@example.com", "", max_attempts=3)

        assert attempts["count"] == 3
        assert result["ok"] is True
        assert result["bundle"]["plan_type"] == "team"
        assert result["attempts"] == 3

    @pytest.mark.parametrize("error_type", ["add_phone", "email_verification", "login_state_lost"])
    def test_single_attempt_failure_types_do_not_same_round_retry(self, monkeypatch, error_type):
        attempts = {"count": 0}

        def fake_login(email, password, mail_client=None, return_result=False):
            assert return_result is True
            attempts["count"] += 1
            return {
                "ok": False,
                "bundle": None,
                "error_type": error_type,
                "error_detail": "terminal for this round",
                "retryable": True,
            }

        monkeypatch.setattr(manager_mod, "login_codex_via_browser", fake_login)

        result = manager_mod._login_codex_with_result("user@example.com", "", max_attempts=3)

        assert attempts["count"] == 1
        assert result["ok"] is False
        assert result["error_type"] == error_type
        assert result["attempts"] == 1

    def test_rejects_non_team_bundle(self, monkeypatch):
        def fake_login(email, password, mail_client=None, return_result=False):
            assert return_result is True
            return {
                "ok": True,
                "bundle": {"email": email, "plan_type": "free"},
                "error_type": None,
                "error_detail": None,
                "retryable": False,
            }

        monkeypatch.setattr(manager_mod, "login_codex_via_browser", fake_login)

        result = manager_mod._login_codex_with_result("user@example.com", "", max_attempts=1)

        assert result["ok"] is False
        assert result["bundle"] is None
        assert result["error_type"] == "non_team_plan"
        assert result["attempts"] == 1


class TestCmdCheckAuthRepairEntry:
    def test_preserves_historical_low_quota_on_network_error_for_remove_first(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "codex-low@example.com-team.json"
        auth_file.write_text('{"access_token": "token"}', encoding="utf-8")
        account = {
            "email": "low@example.com",
            "status": "active",
            "auth_file": str(auth_file),
            "mail_provider": "cloudmail",
            "mail_account_id": 123,
            "last_quota": {
                "primary_pct": 95,
                "primary_resets_at": 2_000,
                "weekly_pct": 1,
                "weekly_resets_at": 0,
            },
        }
        preserved = []
        updates = []

        monkeypatch.setattr(manager_mod, "_reconcile_team_members", lambda: {})
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_000)
        monkeypatch.setattr(manager_mod, "load_accounts", lambda: [dict(account)])
        monkeypatch.setattr(manager_mod, "get_mail_domain", lambda: "example.com")
        monkeypatch.setattr(manager_mod, "_is_main_account_email", lambda _email: False)
        monkeypatch.setattr(manager_mod, "_check_and_refresh", lambda _acc: ("network_error", None))
        monkeypatch.setattr(manager_mod, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

        exhausted = manager_mod.cmd_check(preserve_low_active=True, preserved_low_accounts=preserved)

        assert exhausted == []
        assert preserved == [
            {
                "email": "low@example.com",
                "remaining": 5,
                "quota": account["last_quota"],
            }
        ]
        assert updates == []

    def test_force_auth_repair_ignores_cooldown_for_auth_pending(self, monkeypatch):
        class FakeMailClient:
            provider_name = "cloudmail"

        calls = []
        monkeypatch.setattr(manager_mod, "_reconcile_team_members", lambda: {})
        monkeypatch.setattr(
            manager_mod,
            "load_accounts",
            lambda: [
                {
                    "email": "pending@example.com",
                    "status": "auth_pending",
                    "password": "",
                    "auth_file": None,
                    "mail_provider": "cloudmail",
                    "auth_retry_count": 2,
                    "auth_last_error": "auth_code_missing",
                    "auth_retry_after": 1_700_000_600,
                    "auth_retry_paused": False,
                }
            ],
        )
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_700_000_000)
        monkeypatch.setattr(manager_mod, "_is_main_account_email", lambda _email: False)
        monkeypatch.setattr(manager_mod, "get_mail_domain", lambda: "@example.com")
        monkeypatch.setattr(manager_mod, "_get_account_mail_client", lambda _acc: FakeMailClient())
        monkeypatch.setattr(manager_mod, "_ensure_account_ipv6_proxy", lambda _email: ("", ""))
        monkeypatch.setattr(manager_mod, "is_token_pair_invalidated", lambda _auth_file: False)
        monkeypatch.setattr(manager_mod, "update_account", lambda *args, **kwargs: None)
        monkeypatch.setattr(manager_mod, "_record_auth_repair_failure", lambda *args, **kwargs: {})

        def fake_login(email, password, mail_client=None):
            calls.append((email, password, mail_client.provider_name))
            return {
                "ok": False,
                "bundle": None,
                "error_type": "auth_code_missing",
                "error_detail": "未获取到 auth code",
                "retryable": True,
            }

        monkeypatch.setattr(manager_mod, "_login_codex_with_result", fake_login)

        manager_mod.cmd_check(force_auth_repair=True)

        assert calls == [("pending@example.com", "", "cloudmail")]


# ===========================================================================
# 8. _record_auth_repair_failure (state machine integrated)
# ===========================================================================
class TestRecordAuthRepairFailure:
    """All status writes go through update_account → default_machine.transition.

    We seed an account in tmp accounts.json and assert post-call status + fields.
    """

    @pytest.fixture
    def seeded_account(self, isolated_accounts):
        """Seed one ACTIVE in-team account."""
        accounts_mod.add_account("victim@example.com", "pwd")
        accounts_mod.update_account(
            "victim@example.com",
            status="active",
            workspace_account_id="w1",
        )
        return "victim@example.com"

    def test_transient_exception_decay_retry_preserves_status(
        self, seeded_account, monkeypatch
    ):
        """临时异常 → 衰退式 retry_after, paused=False, 不释放席位也不写 AUTH_INVALID。"""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", False)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(
            manager_mod,
            "_release_auth_repair_team_seat",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("transient failure must not release seat")),
        )

        result = manager_mod._record_auth_repair_failure(
            seeded_account,
            error_type="exception",
            error_detail="Page.screenshot: Timeout 30000ms exceeded.",
        )

        assert result["auth_retry_paused"] is False
        assert result["auth_retry_after"] is not None
        assert result["auth_retry_count"] == 1
        assert result["status"] == accounts_mod.STATUS_ACTIVE
        assert result["seat_released"] is False
        assert result["release_attempted"] is False
        assert result["transient"] is True

        # Verify accounts.json reflects the state
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), seeded_account)
        assert acc["status"] == accounts_mod.STATUS_ACTIVE
        assert acc["auth_last_error"] == "exception"

    def test_hard_failure_pauses_and_releases_seat(
        self, seeded_account, monkeypatch
    ):
        """human_verification → paused=True + 释放席位 → status=STANDBY。"""
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        # Stub out _release_auth_repair_team_seat to "removed"
        release_calls: list[str] = []

        def fake_release(email, *, chatgpt_api=None):
            release_calls.append(email)
            return "removed"

        monkeypatch.setattr(manager_mod, "_release_auth_repair_team_seat", fake_release)

        result = manager_mod._record_auth_repair_failure(
            seeded_account, error_type="human_verification"
        )

        assert result["auth_retry_paused"] is True
        assert result["auth_retry_after"] is None
        assert result["release_attempted"] is True
        assert result["seat_released"] is True
        assert result["remove_status"] == "removed"
        assert result["status"] == accounts_mod.STATUS_STANDBY
        assert release_calls == [seeded_account]

    def test_add_phone_soft_retry_within_limit(
        self, seeded_account, monkeypatch
    ):
        """add_phone (软重试开 + 未超限) → paused=False + retry_after."""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", False)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setenv("AUTO_CHECK_RETRY_ADD_PHONE", "1")
        monkeypatch.setenv("AUTO_CHECK_ADD_PHONE_MAX_RETRIES", "3")

        result = manager_mod._record_auth_repair_failure(
            seeded_account, error_type="add_phone"
        )

        assert result["auth_retry_paused"] is False
        assert result["auth_retry_after"] is not None
        assert result["auth_retry_count"] == 1
        assert result["release_attempted"] is False
        assert result["status"] == accounts_mod.STATUS_AUTH_INVALID

    def test_add_phone_exceeds_limit_pauses_and_releases(
        self, seeded_account, monkeypatch
    ):
        """add_phone 超过 max_retries → paused=True + 释放席位."""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", True)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        # Pre-seed 3 prior add_phone failures
        accounts_mod.update_account(
            seeded_account,
            auth_retry_count=3,
            auth_last_error="add_phone",
        )

        monkeypatch.setattr(
            manager_mod, "_release_auth_repair_team_seat",
            lambda email, **kw: "removed",
        )
        # Force max_retries=3 so 4th attempt triggers paused
        monkeypatch.setattr(manager_mod, "_auth_repair_add_phone_max_retries", lambda: 3)
        monkeypatch.setattr(manager_mod, "_auth_repair_retry_add_phone_enabled", lambda: True)

        result = manager_mod._record_auth_repair_failure(
            seeded_account, error_type="add_phone"
        )

        assert result["auth_retry_paused"] is True
        assert result["release_attempted"] is True
        assert result["status"] == accounts_mod.STATUS_STANDBY
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), seeded_account)
        assert acc["disabled"] is True
        assert acc["reuse_disabled"] is True
        assert acc["retired_reason"] == "auth_repair_failed:add_phone"

    def test_repeated_email_verification_releases_and_disables(
        self, seeded_account, monkeypatch
    ):
        """email_verification exhausts retry budget → release seat and disable reuse."""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", True)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_700_000_000)
        accounts_mod.update_account(
            seeded_account,
            auth_retry_count=2,
            auth_last_error="email_verification",
        )
        monkeypatch.setattr(manager_mod, "_auth_repair_retry_delays", lambda: (120, 240, 360))
        monkeypatch.setattr(manager_mod, "_release_auth_repair_team_seat", lambda email, **kw: "removed")

        result = manager_mod._record_auth_repair_failure(
            seeded_account,
            error_type="email_verification",
            error_detail="卡在邮箱验证码页",
        )

        assert result["auth_retry_count"] == 3
        assert result["auth_retry_paused"] is True
        assert result["auth_retry_after"] is None
        assert result["release_attempted"] is True
        assert result["seat_released"] is True
        assert result["status"] == accounts_mod.STATUS_STANDBY
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), seeded_account)
        assert acc["disabled"] is True
        assert acc["reuse_disabled"] is True
        assert acc["retired_at"] == 1_700_000_000
        assert acc["retired_reason"] == "auth_repair_failed:email_verification"

    def test_login_state_lost_releases_missing_auth_and_disables(
        self, seeded_account, monkeypatch
    ):
        """login_state_lost without local auth is a Team blocker when skip-reuse is enabled."""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", True)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_700_000_000)
        monkeypatch.setattr(manager_mod, "_release_auth_repair_team_seat", lambda email, **kw: "removed")

        result = manager_mod._record_auth_repair_failure(
            seeded_account,
            error_type="login_state_lost",
            error_detail="登录态丢失",
        )

        assert result["auth_retry_count"] == 1
        assert result["auth_retry_paused"] is True
        assert result["auth_retry_after"] is None
        assert result["release_attempted"] is True
        assert result["seat_released"] is True
        assert result["protected_local_credential"] is False
        assert result["status"] == accounts_mod.STATUS_STANDBY
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), seeded_account)
        assert acc["disabled"] is True
        assert acc["retired_reason"] == "auth_repair_failed:login_state_lost"

    @pytest.mark.parametrize(
        "error_type",
        ["non_team_plan", "oauth_timeout", "auth_code_missing"],
    )
    def test_aggressive_login_link_failures_release_child(
        self, seeded_account, monkeypatch, error_type
    ):
        """One-shot login/link failures should release managed child capacity under skip-reuse."""
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", True)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_700_000_000)
        monkeypatch.setattr(
            manager_mod,
            "_release_auth_repair_team_seat",
            lambda email, **kw: "removed",
        )

        result = manager_mod._record_auth_repair_failure(
            seeded_account,
            error_type=error_type,
            error_detail="login/link failure",
        )

        assert result["auth_retry_count"] == 1
        assert result["auth_retry_paused"] is True
        assert result["auth_retry_after"] is None
        assert result["release_attempted"] is True
        assert result["seat_released"] is True
        assert result["status"] == accounts_mod.STATUS_STANDBY
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), seeded_account)
        assert acc["disabled"] is True
        assert acc["reuse_disabled"] is True
        assert acc["retired_reason"] == f"auth_repair_failed:{error_type}"

    def test_login_state_lost_preserves_protected_local_credential(
        self, isolated_accounts, tmp_path, monkeypatch
    ):
        """login_state_lost must not release a manually protected local auth file."""
        auth_file = tmp_path / "manual-seat.json"
        auth_file.write_text("{}", encoding="utf-8")
        accounts_mod.add_account("manual@example.com", "pwd")
        accounts_mod.update_account(
            "manual@example.com",
            status=accounts_mod.STATUS_AUTH_INVALID,
            auth_file=str(auth_file),
            protect_team_seat=True,
        )
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(
            manager_mod,
            "_release_auth_repair_team_seat",
            lambda email, **kw: (_ for _ in ()).throw(AssertionError("protected seat should not be released")),
        )

        result = manager_mod._record_auth_repair_failure(
            "manual@example.com",
            error_type="login_state_lost",
            error_detail="login state lost",
        )

        assert result["auth_retry_count"] == 1
        assert result["auth_retry_paused"] is True
        assert result["auth_retry_after"] is None
        assert result["release_attempted"] is False
        assert result["seat_released"] is False
        assert result["protected_local_credential"] is True
        assert result["status"] == accounts_mod.STATUS_AUTH_INVALID
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), "manual@example.com")
        assert acc["disabled"] is False
        assert acc.get("retired_reason") is None

    def test_auth_error_releases_protected_managed_child(
        self, isolated_accounts, tmp_path, monkeypatch
    ):
        """Managed child seats may be released even when stale protection flags remain."""
        auth_file = tmp_path / "managed-seat.json"
        auth_file.write_text("{}", encoding="utf-8")
        accounts_mod.add_account("managed@example.com", "pwd")
        accounts_mod.update_account(
            "managed@example.com",
            status=accounts_mod.STATUS_ACTIVE,
            auth_file=str(auth_file),
            mail_account_id=123,
            protect_team_seat=True,
        )
        monkeypatch.setattr("autoteam.config.ROTATE_SKIP_REUSE", True)
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: True)
        monkeypatch.setattr(manager_mod.time, "time", lambda: 1_700_000_000)
        monkeypatch.setattr(
            manager_mod,
            "_release_auth_repair_team_seat",
            lambda email, **kw: "removed",
        )

        result = manager_mod._record_auth_repair_failure(
            "managed@example.com",
            error_type="auth_error_discard",
            error_detail="token invalid",
        )

        assert result["auth_retry_paused"] is True
        assert result["protected_local_credential"] is True
        assert result["protected_replacement_override"] is True
        assert result["release_attempted"] is True
        assert result["seat_released"] is True
        assert result["status"] == accounts_mod.STATUS_STANDBY
        acc = accounts_mod.find_account(accounts_mod.load_accounts(), "managed@example.com")
        assert acc["status"] == accounts_mod.STATUS_STANDBY
        assert acc["disabled"] is True
        assert acc["reuse_disabled"] is True
        assert acc["retired_at"] == 1_700_000_000
        assert acc["retired_reason"] == "auth_repair_failed:auth_error_discard"

    def test_failure_when_not_in_team_lands_standby(
        self, seeded_account, monkeypatch
    ):
        """Account not in team and not in seat-status → final_status=STANDBY directly."""
        # Pre-set status to STANDBY so check at end (acc.status not in seat-set) → final=STANDBY
        accounts_mod.update_account(seeded_account, status="standby")
        monkeypatch.setattr(manager_mod, "_is_email_in_team", lambda email: False)

        result = manager_mod._record_auth_repair_failure(
            seeded_account, error_type="login_failed"
        )

        assert result["status"] == accounts_mod.STATUS_STANDBY
        assert result["release_attempted"] is False


# ===========================================================================
# 9. _release_auth_repair_team_seat
# ===========================================================================
class TestReleaseAuthRepairTeamSeat:
    def test_uses_provided_chatgpt_api(self, monkeypatch):
        api = MagicMock()
        api.browser = MagicMock()
        # _chatgpt_session_ready uses getattr(api, "is_started", None) — make it None
        del api.is_started
        monkeypatch.setattr(
            manager_mod, "remove_from_team", lambda *a, **kw: "removed"
        )
        result = manager_mod._release_auth_repair_team_seat(
            "x@example.com", chatgpt_api=api
        )
        assert result == "removed"
        api.start.assert_not_called()
        api.stop.assert_not_called()

    def test_starts_and_stops_when_session_not_ready(self, monkeypatch):
        api_instance = MagicMock()
        api_instance.browser = None  # not ready
        del api_instance.is_started

        # When start() is called, browser becomes truthy
        def _start_side_effect():
            api_instance.browser = MagicMock()

        api_instance.start.side_effect = _start_side_effect
        monkeypatch.setattr(
            manager_mod, "ChatGPTTeamAPI", lambda: api_instance
        )
        monkeypatch.setattr(
            manager_mod, "remove_from_team", lambda *a, **kw: "already_absent"
        )
        result = manager_mod._release_auth_repair_team_seat("x@example.com")
        assert result == "already_absent"
        api_instance.start.assert_called_once()
        api_instance.stop.assert_called_once()

    def test_returns_failed_on_exception(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(manager_mod, "ChatGPTTeamAPI", boom)
        result = manager_mod._release_auth_repair_team_seat("x@example.com")
        assert result == "failed"


# ===========================================================================
# 10. _auth_repair_result_suffix
# ===========================================================================
class TestAuthRepairResultSuffix:
    def test_seat_released_suffix(self):
        result = {"seat_released": True, "auth_retry_paused": True}
        suffix = manager_mod._auth_repair_result_suffix(result)
        assert "已释放 Team 席位" in suffix
        assert "已暂停" in suffix

    def test_release_failed_suffix(self):
        result = {
            "seat_released": False,
            "release_attempted": True,
            "remove_status": "failed",
        }
        suffix = manager_mod._auth_repair_result_suffix(result)
        assert "释放 Team 席位失败" in suffix

    def test_no_release_suffix(self):
        result = {"seat_released": False, "release_attempted": False}
        assert manager_mod._auth_repair_result_suffix(result) == ""
