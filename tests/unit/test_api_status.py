import json
import logging
import threading
import time

from autoteam import account_state, accounts, api, config


def test_get_status_normalizes_main_account_status_from_saved_auth(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text(json.dumps({"access_token": "token-main"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": main_email,
                "status": "exhausted",
                "auth_file": "/app/auths/codex-main.json",
                "last_quota": {
                    "primary_pct": 8,
                    "primary_resets_at": 1710000000,
                    "weekly_pct": 1,
                    "weekly_resets_at": 1710600000,
                },
            }
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda access_token, **_kwargs: (
            "ok",
            {
                "primary_pct": 8,
                "primary_resets_at": 1710000000,
                "weekly_pct": 1,
                "weekly_resets_at": 1710600000,
            },
        ),
    )

    result = api.get_status()

    assert result["quota_cache"][main_email]["primary_pct"] == 8
    assert result["accounts"][0]["is_main_account"] is True
    assert result["accounts"][0]["status"] == "active"
    assert result["summary"] == {
        "active": 1,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "personal": 0,
        "auth_invalid": 0,
        "orphan": 0,
        "disabled": 0,
        "total": 1,
    }


def test_get_status_survives_runtime_resource_probe_failure(monkeypatch):
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(
        api,
        "collect_runtime_resource_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("proc unavailable")),
    )

    result = api.get_status()

    assert result["accounts"] == []
    assert result["runtime_resources"] == {"error": "runtime_resource_snapshot_failed"}


def test_get_status_includes_ipv6_pool_status(monkeypatch):
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(
        "autoteam.ipv6_pool.ipv6_pool.status",
        lambda: {"enabled": True, "required": False, "ok": True, "count": 2, "entries": []},
    )

    result = api.get_status()

    assert result["ipv6_pool"]["enabled"] is True
    assert result["ipv6_pool"]["count"] == 2


def test_get_status_exposes_clipproxy_and_rotation_validation(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-child.json"
    auth_file.write_text(json.dumps({"access_token": "token-child"}), encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "child@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {"primary_pct": 20}))
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda: {
            "ok": False,
            "safe_read_only": True,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": False,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "no_provider_auth",
                "total": 0,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )
    monkeypatch.setattr(
        api,
        "_rotation_validation_cooldown",
        {"next_rotate_after": 0.0, "recorded_at": 123.0, "severity": "ok", "reason": ""},
    )

    result = api.get_status()

    assert result["summary"]["active"] == 1
    assert result["cliproxy"]["safe_read_only"] is True
    assert result["cliproxy"]["provider_auth"]["reason"] == "no_provider_auth"
    assert result["rotation_validation"]["severity"] == "ok"
    assert result["rotation_validation"]["cooldown_remaining_seconds"] == 0


def test_get_status_fast_skips_live_quota_probe(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-child.json"
    auth_file.write_text(json.dumps({"access_token": "token-child"}), encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "child@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fast status must not probe quota")),
    )
    health_calls = []
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **kwargs: health_calls.append(kwargs)
        or {
            "ok": True,
            "safe_read_only": True,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": True,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "provider_auth_has_candidates",
                "total": 1,
                "available": 1,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )

    result = api.get_status(fast=True)

    assert result["quota_cache"] == {}
    assert result["status_mode"] == {"fast": True, "quota_budget_seconds": 0, "live_quota": False}
    assert health_calls == [{"timeout": 0.5, "cache_ttl": 30.0}]


def test_get_status_exposes_live_quota_auth_error(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-child.json"
    auth_file.write_text(
        json.dumps({"access_token": "token-child", "account_id": "acct-child"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "child@example.com",
                "status": accounts.STATUS_ACTIVE,
                "auth_file": str(auth_file),
                "workspace_account_id": "acct-fallback",
                "last_quota": {"primary_pct": 1, "weekly_pct": 0},
            }
        ],
    )
    calls = []
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda token, **kwargs: calls.append((token, kwargs)) or ("auth_error", None),
    )

    result = api.get_status()

    assert calls == [("token-child", {"account_id": "acct-child"})]
    assert result["quota_cache"] == {}
    assert result["live_quota_status"] == {"child@example.com": "auth_error"}
    assert result["accounts"][0]["live_quota_status"] == "auth_error"
    assert result["accounts"][0]["status"] == accounts.STATUS_ACTIVE
    assert "last_quota" not in result["accounts"][0]
    assert result["accounts"][0]["primary_remaining_pct"] is None
    assert result["accounts"][0]["weekly_remaining_pct"] is None


def test_get_status_displays_active_as_exhausted_from_live_quota(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-child.json"
    auth_file.write_text(json.dumps({"access_token": "token-child"}), encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "child@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda *_args, **_kwargs: (
            "exhausted",
            {
                "quota_info": {
                    "primary_pct": 100,
                    "primary_resets_at": 1_800,
                    "weekly_pct": 17,
                    "weekly_resets_at": 9_000,
                }
            },
        ),
    )

    result = api.get_status()

    assert result["accounts"][0]["raw_status"] == accounts.STATUS_ACTIVE
    assert result["accounts"][0]["status"] == accounts.STATUS_EXHAUSTED
    assert result["accounts"][0]["primary_remaining_pct"] == 0
    assert result["summary"]["active"] == 0
    assert result["summary"]["exhausted"] == 1


def test_get_playwright_context_options_uses_fingerprint_constants(monkeypatch):
    monkeypatch.setattr(config, "PLAYWRIGHT_USER_AGENT", "AutoTeamTest/1.0")
    monkeypatch.setattr(config, "PLAYWRIGHT_LOCALE", "zh-CN")
    monkeypatch.setattr(config, "PLAYWRIGHT_TIMEZONE_ID", "Asia/Shanghai")
    monkeypatch.setattr(config, "PLAYWRIGHT_VIEWPORT_WIDTH", 1440)
    monkeypatch.setattr(config, "PLAYWRIGHT_VIEWPORT_HEIGHT", 900)
    monkeypatch.setattr(config, "PLAYWRIGHT_DEVICE_SCALE_FACTOR", 2)
    monkeypatch.setattr(config, "PLAYWRIGHT_COLOR_SCHEME", "light")

    options = config.get_playwright_context_options()

    assert options["viewport"] == {"width": 1440, "height": 900}
    assert options["user_agent"] == "AutoTeamTest/1.0"
    assert options["locale"] == "zh-CN"
    assert options["timezone_id"] == "Asia/Shanghai"
    assert options["device_scale_factor"] == 2
    assert options["color_scheme"] == "light"
    assert options["extra_http_headers"] == {"Accept-Language": "zh-CN,zh;q=0.9"}


def test_auto_check_cooldown_does_not_delay_real_team_shortage(tmp_path, monkeypatch):

    auth_files = []
    for idx in range(1):
        auth_file = tmp_path / f"active-{idx}.json"
        auth_file.write_text(json.dumps({"access_token": f"token-{idx}"}), encoding="utf-8")
        auth_files.append(auth_file)

    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", time.time())
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda _target: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": f"active-{idx}@example.com", "status": "active", "auth_file": str(auth_files[idx])}
            for idx in range(1)
        ],
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-fill"
    assert params == {"target_seats": 3}
    assert args == (3,)
    assert kwargs == {"background_post_sync": True}


def test_auto_check_cooldown_keeps_full_team_from_refilling(tmp_path, monkeypatch):

    auth_files = []
    for idx in range(1):
        auth_file = tmp_path / f"active-{idx}.json"
        auth_file.write_text(json.dumps({"access_token": f"token-{idx}"}), encoding="utf-8")
        auth_files.append(auth_file)

    started = []
    probed = []

    def fake_team_count(*args, **kwargs):
        probed.append((args, kwargs))
        return 3

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", time.time())
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda _target: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", fake_team_count)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": f"active-{idx}@example.com", "status": "active", "auth_file": str(auth_files[idx])}
            for idx in range(1)
        ],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda _token: ("ok", {"primary_pct": 10, "primary_resets_at": 0, "weekly_pct": 1}),
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert probed
    assert started == []


def test_auto_check_burn_guard_defaults_are_not_overly_strict():
    assert config.AUTO_CHECK_BURN_GUARD_ENABLED is True
    assert config.AUTO_CHECK_BURN_GUARD_WINDOW_SECONDS == 3600
    assert config.AUTO_CHECK_BURN_GUARD_MAX_EXHAUSTED == 4
    assert config.AUTO_CHECK_BURN_GUARD_COOLDOWN_SECONDS == 300
    assert config.AUTO_CHECK_AUTH_REPAIR_GUARD_ENABLED is True
    assert config.AUTO_CHECK_AUTH_REPAIR_GUARD_WINDOW_SECONDS == 3600
    assert config.AUTO_CHECK_AUTH_REPAIR_GUARD_MAX_INVALID == 4
    assert config.AUTO_CHECK_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS == 300


def test_auto_check_burn_guard_detects_cluster_from_state_log(tmp_path, monkeypatch):
    state_log = tmp_path / "state_log.jsonl"
    state_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "email": "one@example.com",
                        "from_state": "active",
                        "to_state": "exhausted",
                        "timestamp": 950,
                    }
                ),
                json.dumps(
                    {
                        "email": "two@example.com",
                        "from_state": "active",
                        "to_state": "exhausted",
                        "timestamp": 980,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(account_state, "DEFAULT_LOG_PATH", state_log)
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api, "_DEFAULT_BURN_GUARD_ENABLED", True)
    monkeypatch.setattr(api, "_DEFAULT_BURN_GUARD_WINDOW_SECONDS", 120)
    monkeypatch.setattr(api, "_DEFAULT_BURN_GUARD_MAX_EXHAUSTED", 2)
    monkeypatch.setattr(api, "_DEFAULT_BURN_GUARD_COOLDOWN_SECONDS", 600)

    status = api._auto_check_burn_guard_status([], now=1_000)

    assert status["blocked"] is True
    assert status["count"] == 2
    assert status["emails"] == ["one@example.com", "two@example.com"]
    assert status["remaining_seconds"] == 580


def test_auto_check_auth_repair_guard_detects_exception_cluster(tmp_path, monkeypatch):
    state_log = tmp_path / "state_log.jsonl"
    state_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "email": f"child-{idx}@example.com",
                        "from_state": "active",
                        "to_state": "auth_invalid",
                        "reason": "auth_repair:exception",
                        "timestamp": 900 + idx * 10,
                    }
                )
                for idx in range(4)
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(account_state, "DEFAULT_LOG_PATH", state_log)
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api, "_DEFAULT_AUTH_REPAIR_GUARD_ENABLED", True)
    monkeypatch.setattr(api, "_DEFAULT_AUTH_REPAIR_GUARD_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(api, "_DEFAULT_AUTH_REPAIR_GUARD_MAX_INVALID", 4)
    monkeypatch.setattr(api, "_DEFAULT_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS", 300)

    status = api._auto_check_auth_repair_guard_status(now=1_000)

    assert status["blocked"] is True
    assert status["count"] == 4
    assert status["remaining_seconds"] == 230


def test_auth_repair_recovery_candidates_only_match_screenshot_misclassification(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-yun@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    rows = api._auth_repair_recovery_candidates(
        [
            {
                "email": "yun@example.com",
                "status": "auth_invalid",
                "auth_last_error": "exception",
                "auth_last_error_detail": "Page.screenshot: Timeout 30000ms exceeded.",
                "auth_file": str(auth_file),
            },
            {
                "email": "hard@example.com",
                "status": "auth_invalid",
                "auth_last_error": "non_team_plan",
                "auth_last_error_detail": "free",
                "auth_file": str(auth_file),
            },
            {
                "email": "owner@example.com",
                "status": "auth_invalid",
                "auth_last_error": "exception",
                "auth_last_error_detail": "Page.screenshot: Timeout 30000ms exceeded.",
                "auth_file": str(auth_file),
            },
        ]
    )

    assert [row["email"] for row in rows] == ["yun@example.com"]


def test_auth_repair_recover_misclassified_apply_restores_standby(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-yun@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")
    updates = []
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "yun@example.com",
                "status": "auth_invalid",
                "auth_last_error": "exception",
                "auth_last_error_detail": "Page.screenshot: Timeout 30000ms exceeded.",
                "auth_file": str(auth_file),
            }
        ],
    )
    monkeypatch.setattr("autoteam.accounts.update_account", lambda email, **fields: updates.append((email, fields)))

    result = api.post_auth_repair_recover_misclassified(
        api.AuthRepairRecoverParams(apply=True, emails=["yun@example.com"])
    )

    assert result["restored"] == ["yun@example.com"]
    assert updates == [
        (
            "yun@example.com",
            {
                "status": "standby",
                "auth_retry_count": 0,
                "auth_last_error": None,
                "auth_last_error_detail": None,
                "auth_last_failed_at": None,
                "auth_retry_after": None,
                "auth_retry_paused": False,
                "_reason": "recover_auth_repair_misclassification",
            },
        )
    ]


def test_auto_check_burn_guard_blocks_auto_fill(tmp_path, monkeypatch, caplog):
    auth_file = tmp_path / "active.json"
    auth_file.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    started = []

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda _target: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(
        api,
        "_auto_check_burn_guard_status",
        lambda _accounts: {
            "blocked": True,
            "window_seconds": 3600,
            "count": 2,
            "emails": ["old-one@example.com", "old-two@example.com"],
            "remaining_seconds": 300,
        },
    )
    monkeypatch.setattr(api, "_auto_check_auth_repair_guard_status", lambda: {"blocked": False})
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "active@example.com", "status": "active", "auth_file": str(auth_file)}],
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    with caplog.at_level(logging.WARNING):
        api._auto_check_loop()

    assert started == []
    assert "烧号熔断中，本轮跳过 auto-fill" in caplog.text


def test_auto_check_burn_guard_blocks_auto_replace_after_marking_exhausted(tmp_path, monkeypatch, caplog):
    auth_file = tmp_path / "active.json"
    auth_file.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    started = []
    updates = []

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 2, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda _target: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(
        api,
        "_auto_check_burn_guard_status",
        lambda _accounts: {
            "blocked": True,
            "window_seconds": 3600,
            "count": 2,
            "emails": ["old-one@example.com", "old-two@example.com"],
            "recent_window_emails": ["old-one@example.com", "old-two@example.com"],
            "remaining_seconds": 300,
        },
    )
    monkeypatch.setattr(api, "_auto_check_auth_repair_guard_status", lambda: {"blocked": False})
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "active@example.com", "status": "active", "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda _token: ("exhausted", {}))
    monkeypatch.setattr("autoteam.accounts.update_account", lambda email, **fields: updates.append((email, fields)))

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    with caplog.at_level(logging.WARNING):
        api._auto_check_loop()

    assert started == []
    assert updates and updates[0][0] == "active@example.com"
    assert updates[0][1]["status"] == accounts.STATUS_EXHAUSTED
    assert "烧号熔断阻止 auto-replace" in caplog.text


def test_auto_check_team_member_count_carries_invites_and_occupancy(monkeypatch):
    monkeypatch.setattr(api, "_run_playwright_probe", lambda *args, **kwargs: {"count": 3, "invites": 1, "occupancy": 4})

    result = api._auto_check_team_member_count()

    assert result == 3
    assert api._team_member_invite_count(result) == 1
    assert api._team_member_occupancy(result) == 4


def test_auto_check_skips_expensive_probe_when_task_is_running(monkeypatch, caplog):
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: (_ for _ in ()).throw(AssertionError("busy auto-check must not load accounts")),
    )
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("busy auto-check must not query CPA gate")),
    )

    stop_event = api._auto_check_stop
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    api._playwright_lock.acquire()
    try:
        with caplog.at_level(logging.INFO):
            api._auto_check_loop()
    finally:
        if api._playwright_lock.locked():
            api._playwright_lock.release()

    assert "跳过本轮自动巡检昂贵探测" in caplog.text


def test_auto_check_cooldown_allows_full_team_blocker_replacement(monkeypatch):
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", time.time())
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda _target: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 3)
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "healthy@example.com", "status": "active", "auth_file": ""},
            {"email": "blocked@example.com", "status": "auth_invalid", "auth_file": ""},
        ],
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-fill"
    assert params == {"target_seats": 3}
    assert args == (3,)
    assert kwargs == {"background_post_sync": True}


def test_auto_check_uses_read_only_cpa_gate_when_team_full_and_no_credentials(monkeypatch, caplog):
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 3)
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda target: target == "cpa")
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **_kwargs: {
            "ok": False,
            "safe_read_only": True,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": False,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "no_provider_auth",
                "total": 0,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.sync_to_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("CPA gate must not sync")),
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.upload_to_cpa",
        lambda _path: (_ for _ in ()).throw(AssertionError("CPA gate must not upload")),
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.delete_from_cpa",
        lambda _name: (_ for _ in ()).throw(AssertionError("CPA gate must not delete")),
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    with caplog.at_level(logging.INFO):
        api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-fill"
    assert params == {
        "target_seats": 3,
        "trigger": "auto-check",
        "shortage": 2,
        "cpa_credential_gate": {
            "enabled": True,
            "safe_read_only": True,
            "management_ok": True,
            "available": 0,
            "total": 0,
            "reason": "no_provider_auth",
            "zero_available": True,
        },
        "provider_auth_below_target": True,
        "provider_auth_available": 0,
        "provider_auth_target": 2,
    }
    assert args == (3,)
    assert kwargs == {"background_post_sync": True}
    assert "CPA 可用凭证 gate" in caplog.text
    assert "CPA 可用凭证为 0/0" in caplog.text


def test_auto_check_triggers_cleanup_when_pending_invite_exceeds_occupancy(monkeypatch):
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(
        api,
        "_auto_check_team_member_count",
        lambda *args, **kwargs: api._TeamMemberCount(3, invites=1, occupancy=4),
    )
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda target: False)

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-cleanup"
    assert params["max_seats"] == 3
    assert params["trigger"] == "auto-check"
    assert params["team_count"] == 3
    assert params["invite_count"] == 1
    assert params["team_occupancy"] == 4
    assert args == (3,)
    assert kwargs == {}


def test_auto_check_triggers_preventive_rotate_when_provider_auth_below_target(tmp_path, monkeypatch, caplog):
    auth_ok = tmp_path / "active-ok.json"
    auth_other = tmp_path / "active-other.json"
    auth_ok.write_text(json.dumps({"access_token": "token-ok"}), encoding="utf-8")
    auth_other.write_text(json.dumps({"access_token": "token-other"}), encoding="utf-8")
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "one@example.com", "status": "active", "auth_file": str(auth_ok)},
            {"email": "two@example.com", "status": "active", "auth_file": str(auth_other)},
        ],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda _token: ("ok", {"primary_pct": 10, "primary_resets_at": 1234567890, "weekly_pct": 1}),
    )
    monkeypatch.setattr(
        api,
        "_auto_check_team_member_count",
        lambda *args, **kwargs: api._TeamMemberCount(3, invites=0, occupancy=3),
    )
    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda target: target == "cpa")
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **_kwargs: {
            "ok": True,
            "safe_read_only": True,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": True,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "provider_auth_has_candidates",
                "total": 4,
                "available": 1,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    with caplog.at_level(logging.INFO):
        api._auto_check_loop()

    assert len(started) == 1
    command, params, args, kwargs = started[0]
    assert command == "auto-rotate"
    assert params["target"] == 3
    assert params["trigger"] == "auto-check"
    assert params["shortage"] == 1
    assert params["low_accounts"] == 0
    assert params["provider_auth_below_target"] is True
    assert params["provider_auth_available"] == 1
    assert params["provider_auth_target"] == 2
    assert params["cpa_credential_gate"]["available"] == 1
    assert args == (3,)
    assert kwargs == {"background_post_sync": True}
    assert "provider-auth 可用凭证低水位" in caplog.text


def test_auto_check_cpa_gate_does_not_treat_management_failure_as_zero_credentials(monkeypatch, caplog):
    started = []

    monkeypatch.setattr(api, "_auto_fill_last_trigger_ts", 0.0)
    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "log_runtime_resource_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 3)
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda target: target == "cpa")
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **_kwargs: {
            "ok": False,
            "safe_read_only": True,
            "management_api": {"ok": False, "reason": "request_failed"},
            "provider_auth": {
                "ok": False,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "management_api_unavailable",
                "total": 0,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )

    stop_event = threading.Event()
    restart_event = threading.Event()
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    monkeypatch.setattr(api, "_auto_check_stop", stop_event)
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)

    with caplog.at_level(logging.INFO):
        api._auto_check_loop()

    assert started == []
    assert "CPA 可用凭证 gate" in caplog.text
    assert "management_ok=False" in caplog.text


def test_collect_cpa_credential_gate_requires_safe_read_only_for_zero_available(monkeypatch):
    monkeypatch.setattr("autoteam.sync_targets.is_sync_target_enabled", lambda target: target == "cpa")
    monkeypatch.setattr(
        "autoteam.cliproxy_health.get_cliproxy_health",
        lambda **_kwargs: {
            "ok": True,
            "safe_read_only": False,
            "management_api": {"ok": True},
            "provider_auth": {
                "ok": True,
                "provider": "codex",
                "model": "gpt-5.5",
                "reason": "provider_auth_has_candidates",
                "total": 2,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
        },
    )

    gate = api._collect_cpa_credential_gate()

    assert gate["safe_read_only"] is False
    assert gate["zero_available"] is False
    assert api._cpa_provider_auth_below_pool_target(gate, 2) is False


def test_runtime_validation_degrades_when_provider_auth_below_target(tmp_path, monkeypatch):
    auth_one = tmp_path / "one.json"
    auth_two = tmp_path / "two.json"
    auth_one.write_text(json.dumps({"access_token": "token-one"}), encoding="utf-8")
    auth_two.write_text(json.dumps({"access_token": "token-two"}), encoding="utf-8")

    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "target_seats": 3, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api, "_auto_check_team_member_count", lambda *args, **kwargs: 3)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "one@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_one)},
            {"email": "two@example.com", "status": accounts.STATUS_ACTIVE, "auth_file": str(auth_two)},
        ],
    )
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda _token: ("ok", {"primary_pct": 10}))
    monkeypatch.setattr(
        api,
        "_collect_cpa_credential_gate",
        lambda: {
            "enabled": True,
            "safe_read_only": True,
            "management_ok": True,
            "available": 1,
            "total": 4,
            "reason": "provider_auth_has_candidates",
            "zero_available": False,
        },
    )

    validation = api._log_task_runtime_validation("auto-rotate")

    assert validation["ok"] is False
    assert validation["severity"] == "degraded"
    assert validation["operation_success"] is True
    assert validation["pool_health_ok"] is False
    assert validation["followup_required"] is True
    assert validation["provider_auth_available"] == 1
    assert validation["reason"] == "provider_auth_available=1/2"
    assert validation["cpa_credential_gate"]["available"] == 1


def test_sanitize_account_keeps_exportable_main_account_active_without_live_quota(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))

    sanitized = api._sanitize_account(
        {"email": main_email, "status": "exhausted", "auth_file": "/app/auths/missing.json"}
    )

    assert sanitized["is_main_account"] is True
    assert sanitized["status"] == "active"


def test_sanitize_account_masks_disabled_non_main_status(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    sanitized = api._sanitize_account({"email": "user@example.com", "status": "active", "disabled": True})

    assert sanitized["raw_status"] == "active"
    assert sanitized["status"] == "disabled"
    assert sanitized["disabled"] is True


def test_sanitize_account_exposes_next_usable_for_standby(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    sanitized = api._sanitize_account(
        {
            "email": "standby@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 1_600,
            "last_quota": {"primary_pct": 100, "primary_resets_at": 1_600},
        }
    )

    assert sanitized["next_usable_at"] == 1_600
    assert sanitized["next_usable_reason"] == "quota_cooldown"
    assert sanitized["pool_sort_bucket"] == 40
    assert sanitized["pool_rank_detail"] == "冷却中 10m"


def test_sanitize_account_does_not_let_stale_quota_unlock_cooldown(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    sanitized = api._sanitize_account(
        {
            "email": "stale-cooldown@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 1_600,
            "last_quota": {
                "primary_pct": 1,
                "primary_resets_at": 1_600,
                "weekly_pct": 37,
                "weekly_resets_at": 3_000,
            },
        }
    )

    assert sanitized["primary_remaining_pct"] is None
    assert sanitized["weekly_remaining_pct"] is None
    assert sanitized["next_usable_at"] == 1_600
    assert sanitized["next_usable_reason"] == "quota_cooldown"
    assert sanitized["pool_sort_bucket"] == 40
    assert sanitized["pool_rank_detail"] == "冷却中 10m"


def test_sanitize_account_hides_untrusted_standby_last_quota(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    sanitized = api._sanitize_account(
        {
            "email": "new-standby@example.com",
            "status": accounts.STATUS_STANDBY,
            "last_quota": {
                "primary_pct": 5,
                "primary_resets_at": 1_783_780_927,
                "weekly_pct": 0,
                "weekly_resets_at": 0,
            },
        }
    )

    assert "last_quota" not in sanitized
    assert sanitized["quota_display_scope"] == "standby_snapshot"
    assert sanitized["primary_remaining_pct"] is None
    assert sanitized["weekly_remaining_pct"] is None


def test_sanitize_account_marks_primary_low_without_using_stale_cooldown(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)
    monkeypatch.setattr(config, "AUTO_CHECK_THRESHOLD", 11)

    sanitized = api._sanitize_account(
        {
            "email": "primary-low@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 1_600,
            "last_quota": {
                "primary_pct": 98,
                "primary_resets_at": 1_600,
                "weekly_pct": 52,
                "weekly_resets_at": 3_000,
            },
        }
    )

    assert sanitized["primary_remaining_pct"] is None
    assert sanitized["next_usable_reason"] == "quota_cooldown"
    assert sanitized["next_usable_at"] == 1_600
    assert sanitized["pool_sort_bucket"] == 40
    assert sanitized["pool_rank_detail"] == "冷却中 10m"


def test_sanitize_account_exposes_quota_rank_detail(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    sanitized = api._sanitize_account(
        {
            "email": "ready@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 900,
            "last_quota": {"primary_pct": 42, "weekly_pct": 65},
        }
    )

    assert sanitized["primary_remaining_pct"] is None
    assert sanitized["weekly_remaining_pct"] is None
    assert sanitized["pool_rank_detail"] == "可复用"


def test_sanitize_account_marks_weekly_exhausted_from_trusted_standby_snapshot(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    sanitized = api._sanitize_account(
        {
            "email": "weekly-empty@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 900,
            "standby_quota_snapshot": {"primary_pct": 0, "weekly_pct": 100, "weekly_resets_at": 2_000},
            "quota_snapshot_recorded_at": 900,
        }
    )

    assert sanitized["weekly_remaining_pct"] == 0
    assert sanitized["next_usable_reason"] == "weekly_exhausted"
    assert sanitized["next_usable_at"] == 2_000
    assert sanitized["pool_sort_bucket"] == 55


def test_sanitize_account_marks_ready_low_weekly_without_blocking_reuse(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)
    monkeypatch.setattr(config, "AUTO_CHECK_THRESHOLD", 10)

    sanitized = api._sanitize_account(
        {
            "email": "low-weekly@example.com",
            "status": accounts.STATUS_STANDBY,
            "quota_resets_at": 900,
            "standby_quota_snapshot": {"primary_pct": 0, "weekly_pct": 95},
            "quota_snapshot_recorded_at": 900,
        }
    )

    assert sanitized["next_usable_reason"] == "ready_low_weekly"
    assert sanitized["next_usable_at"] is None
    assert sanitized["pool_sort_bucket"] == 20
    assert "周额度偏低" in sanitized["pool_rank_detail"]


def test_account_pool_sort_orders_active_ready_waiting_and_disabled(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    rows = [
        api._sanitize_account({"email": "disabled@example.com", "status": accounts.STATUS_STANDBY, "disabled": True}),
        api._sanitize_account({"email": "waiting@example.com", "status": accounts.STATUS_STANDBY, "quota_resets_at": 1_500}),
        api._sanitize_account({"email": "active@example.com", "status": accounts.STATUS_ACTIVE, "last_quota": {"primary_pct": 5, "primary_resets_at": 2_000}}),
        api._sanitize_account({"email": "ready@example.com", "status": accounts.STATUS_STANDBY, "quota_resets_at": 900}),
    ]

    rows.sort(key=api._account_pool_sort_key)

    assert [row["email"] for row in rows] == [
        "active@example.com",
        "ready@example.com",
        "waiting@example.com",
        "disabled@example.com",
    ]


def test_account_pool_sort_orders_ready_standby_fifo(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(api.time, "time", lambda: 1_000)

    rows = [
        api._sanitize_account(
            {
                "email": "low-weekly@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": 900,
                "standby_since": 100,
                "last_quota": {"primary_pct": 0, "weekly_pct": 80},
            }
        ),
        api._sanitize_account(
            {
                "email": "high-weekly-low-primary@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": 900,
                "standby_since": 200,
                "last_quota": {"primary_pct": 70, "weekly_pct": 10},
            }
        ),
        api._sanitize_account(
            {
                "email": "high-weekly-high-primary@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": 900,
                "standby_since": 300,
                "last_quota": {"primary_pct": 20, "weekly_pct": 10},
            }
        ),
        api._sanitize_account(
            {
                "email": "waiting-best-quota@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": 1_500,
                "last_quota": {"primary_pct": 100, "primary_resets_at": 1_500, "weekly_pct": 0},
            }
        ),
    ]

    rows.sort(key=api._account_pool_sort_key)

    assert [row["email"] for row in rows] == [
        "low-weekly@example.com",
        "high-weekly-low-primary@example.com",
        "high-weekly-high-primary@example.com",
        "waiting-best-quota@example.com",
    ]


def test_post_rotate_runs_final_sync_in_background(monkeypatch):
    started = []
    rotate_calls = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, func, params, args, kwargs))
        return {"task_id": "rotate-task", "command": command, "params": params}

    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(
        "autoteam.manager.cmd_rotate",
        lambda *args, **kwargs: rotate_calls.append((args, kwargs)),
    )

    result = api.post_rotate(api.TaskParams(target=3))

    assert result["task_id"] == "rotate-task"
    assert len(started) == 1
    command, func, params, args, kwargs = started[0]
    assert command == "rotate"
    assert params == {"target": 3}
    assert args == (3,)
    assert kwargs == {}

    func(3)
    assert rotate_calls == [((3,), {"force_auth_repair": False, "background_post_sync": True})]


def test_post_fill_defers_sync_and_status_to_background(monkeypatch):
    started = []
    fill_calls = []
    sync_calls = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, func, params, args, kwargs))
        return {"task_id": "fill-task", "command": command, "params": params}

    monkeypatch.setattr(api, "_start_task", fake_start_task)
    monkeypatch.setattr(api._pw_executor, "run", lambda fn: (True, "ok", {}))
    monkeypatch.setattr(
        "autoteam.manager.cmd_fill",
        lambda *args, **kwargs: fill_calls.append((args, kwargs)) or {"filled": True},
    )
    monkeypatch.setattr(
        "autoteam.manager._schedule_post_task_sync",
        lambda stage_label: sync_calls.append(stage_label),
    )

    result = api.post_fill(api.TaskParams(target=3))

    assert result["task_id"] == "fill-task"
    assert len(started) == 1
    command, func, params, args, kwargs = started[0]
    assert command == "fill"
    assert params == {"target": 3, "leave_workspace": False}
    assert args == (3,)
    assert kwargs == {"leave_workspace": False}

    assert func(3, leave_workspace=False) == {"filled": True}
    assert fill_calls == [
        ((3,), {"leave_workspace": False, "post_sync": False, "print_status": False})
    ]
    assert sync_calls == ["[填充]"]


def test_disable_and_enable_account_toggle_local_flag(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "member@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    disabled_result = api.post_disable_account("member@example.com")
    enabled_result = api.post_enable_account("member@example.com")

    assert disabled_result["disabled"] is True
    assert disabled_result["account"]["status"] == "disabled"
    assert disabled_result["account"]["raw_status"] == "standby"
    assert enabled_result["disabled"] is False
    assert enabled_result["account"]["status"] == "standby"
    assert accounts.find_account(accounts.load_accounts(), "member@example.com")["disabled"] is False


def test_bulk_disable_accounts_updates_multiple_rows_and_skips_non_targets(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": False},
            {"email": "second@example.com", "status": "active", "disabled": False},
            {"email": "already@example.com", "status": "standby", "disabled": True},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    result = api.post_bulk_disable_accounts(
        api.AccountDisableParams(
            emails=[
                "first@example.com",
                "second@example.com",
                "already@example.com",
                "owner@example.com",
                "missing@example.com",
                "first@example.com",
            ]
        )
    )

    stored = {acc["email"]: acc for acc in accounts.load_accounts()}

    assert result["updated_count"] == 2
    assert result["updated_emails"] == ["first@example.com", "second@example.com"]
    assert result["unchanged_emails"] == ["already@example.com"]
    assert result["skipped_main_accounts"] == ["owner@example.com"]
    assert result["missing_emails"] == ["missing@example.com"]
    assert stored["first@example.com"]["disabled"] is True
    assert stored["second@example.com"]["disabled"] is True
    assert stored["already@example.com"]["disabled"] is True
    assert stored["owner@example.com"]["disabled"] is False


def test_bulk_enable_accounts_updates_disabled_rows_only(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": True},
            {"email": "second@example.com", "status": "active", "disabled": True},
            {"email": "already@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    result = api.post_bulk_enable_accounts(
        api.AccountDisableParams(
            emails=[
                "first@example.com",
                "second@example.com",
                "already@example.com",
                "owner@example.com",
                "missing@example.com",
            ]
        )
    )

    stored = {acc["email"]: acc for acc in accounts.load_accounts()}

    assert result["updated_count"] == 2
    assert result["updated_emails"] == ["first@example.com", "second@example.com"]
    assert result["unchanged_emails"] == ["already@example.com"]
    assert result["skipped_main_accounts"] == ["owner@example.com"]
    assert result["missing_emails"] == ["missing@example.com"]
    assert stored["first@example.com"]["disabled"] is False
    assert stored["second@example.com"]["disabled"] is False
    assert stored["already@example.com"]["disabled"] is False
    assert stored["owner@example.com"]["disabled"] is False


def test_run_task_records_progress_history(monkeypatch):
    task_id = "progresshist"
    api._tasks[task_id] = {
        "task_id": task_id,
        "command": "auto-rotate",
        "params": {},
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "last_progress_at": time.time(),
        "progress": "pending",
        "progress_history": [],
    }
    monkeypatch.setattr(api, "_log_task_runtime_validation", lambda _command: {"ok": True, "severity": "ok"})

    def fake_work():
        api.bump_task_progress("rotate:sync_team")
        api.bump_task_progress("rotate:check_quota")
        return {"ok": True}

    try:
        api._run_task(task_id, fake_work)

        task = api._tasks[task_id]
        stages = [item["stage"] for item in task["progress_history"]]
        assert stages == ["started", "rotate:sync_team", "rotate:check_quota", "runtime_validation", "finished"]
        assert task["progress"] == "rotate:check_quota"
        assert task["progress_history"][0]["duration_sec"] >= 0
        assert task["progress_history"][-1]["stage"] == "finished"
    finally:
        api._tasks.pop(task_id, None)


def test_get_status_counts_disabled_and_skips_disabled_quota_checks(tmp_path, monkeypatch):
    enabled_auth = tmp_path / "enabled.json"
    disabled_auth = tmp_path / "disabled.json"
    enabled_auth.write_text(json.dumps({"access_token": "token-enabled"}), encoding="utf-8")
    disabled_auth.write_text(json.dumps({"access_token": "token-disabled"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "enabled@example.com", "status": "active", "auth_file": str(enabled_auth), "disabled": False},
            {"email": "disabled@example.com", "status": "active", "auth_file": str(disabled_auth), "disabled": True},
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    seen_tokens = []

    def fake_check_quota(access_token, **_kwargs):
        seen_tokens.append(access_token)
        return (
            "ok",
            {
                "primary_pct": 15,
                "primary_resets_at": 1710000000,
                "weekly_pct": 5,
                "weekly_resets_at": 1710600000,
            },
        )

    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_check_quota)

    result = api.get_status()

    assert seen_tokens == ["token-enabled"]
    assert {item["email"]: item["status"] for item in result["accounts"]} == {
        "enabled@example.com": "active",
        "disabled@example.com": "disabled",
    }
    assert result["summary"] == {
        "active": 1,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "personal": 0,
        "auth_invalid": 0,
        "orphan": 0,
        "disabled": 1,
        "total": 2,
    }


def test_post_setup_save_keeps_cpa_url_required_and_generates_api_key(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cloudmail", lambda: True)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda: True)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "generated-token")
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = api.post_setup_save(
        api.SetupConfig(
            CLOUDMAIL_BASE_URL="http://mail.example.com",
            CLOUDMAIL_EMAIL="admin@example.com",
            CLOUDMAIL_PASSWORD="secret",
            CLOUDMAIL_DOMAIN="@example.com",
            CPA_URL="",
            CPA_KEY="key-1",
            PLAYWRIGHT_PROXY_URL="",
            PLAYWRIGHT_PROXY_BYPASS="",
            API_KEY="",
        )
    )

    assert written["CPA_URL"] == "http://127.0.0.1:8317"
    assert written["API_KEY"] == "generated-token"
    assert result["api_key"] == "generated-token"
    assert api.API_KEY == "generated-token"


def test_post_setup_save_normalizes_rotation_account_strategy(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cloudmail", lambda: True)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda: True)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.post_setup_save(
        api.SetupConfig(
            CLOUDMAIL_BASE_URL="http://mail.example.com",
            CLOUDMAIL_PASSWORD="secret",
            CLOUDMAIL_DOMAIN="@example.com",
            CPA_URL="http://127.0.0.1:8317",
            CPA_KEY="key-1",
            API_KEY="old-key",
            ROTATE_NEW_ACCOUNT_MODE="DOMAIN_AUTO_JOIN_FIRST",
            AUTOTEAM_AUTO_JOIN_DOMAINS="@Example.com, other.example.",
            ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE=True,
        )
    )

    assert result["message"] == "配置保存成功"
    assert written["ROTATE_NEW_ACCOUNT_MODE"] == "domain_auto_join_first"
    assert written["AUTOTEAM_AUTO_JOIN_DOMAINS"] == "example.com,other.example"
    assert written["ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE"] == "true"


def test_post_setup_save_rejects_invalid_rotation_account_strategy(monkeypatch):
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "generated-token")

    result = api.post_setup_save(
        api.SetupConfig(
            ROTATE_NEW_ACCOUNT_MODE="invite-maybe",
            ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE="maybe",
        )
    )

    assert result.status_code == 400
    body = json.loads(result.body.decode("utf-8"))
    assert body["message"] == "ROTATE_NEW_ACCOUNT_MODE 必须是 domain_auto_join_first、invite_first 或 direct_first"
