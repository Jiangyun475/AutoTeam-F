from autoteam import manager
import base64
import json


class _FakeChatGPT:
    def __init__(self):
        self.browser = True
        self.started = 0
        self.stopped = 0

    def start(self):
        self.browser = True
        self.started += 1

    def stop(self):
        self.browser = False
        self.stopped += 1


class _FakeMailClient:
    def login(self):
        return None


class _FakeTeamApi:
    def __init__(self, members):
        self.members = members
        self.deleted = []

    def _api_fetch(self, method, path, body=None):
        if method == "GET" and path.endswith("/users"):
            return {"status": 200, "body": json.dumps({"items": self.members})}
        if method == "DELETE" and "/users/" in path:
            self.deleted.append(path.rsplit("/", 1)[-1])
            return {"status": 200, "body": "{}"}
        return {"status": 404, "body": "{}"}


def _jwt_with_chatgpt_user_id(user_id):
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user_id,
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_remove_from_team_does_not_match_hidden_email_by_display_name(monkeypatch):
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_email_matches_current_mail_domain", lambda email: email.endswith("@yunfei.life"))
    monkeypatch.setattr(manager, "_chatgpt_user_id_for_email", lambda _email: "")

    api = _FakeTeamApi(
        [
            {"id": "owner-1", "email": "owner@example.com", "role": "account-owner", "name": "Owner"},
            {"id": "user-hidden", "email": None, "role": "standard-user", "name": "ten"},
        ]
    )

    result = manager.remove_from_team(
        api,
        "yun10@yunfei.life",
        return_status=True,
        lookup_retries=0,
        retry_interval=0,
    )

    assert result == "hidden_unmatched"
    assert api.deleted == []


def test_remove_from_team_matches_hidden_email_by_auth_user_id(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-yun10@yunfei.life-team.json"
    auth_file.write_text(
        json.dumps({"access_token": _jwt_with_chatgpt_user_id("user-hidden")}),
        encoding="utf-8",
    )

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "yun10@yunfei.life", "auth_file": str(auth_file)}],
    )

    api = _FakeTeamApi(
        [
            {"id": "owner-1", "email": "owner@example.com", "role": "account-owner", "name": "Owner"},
            {"id": "user-hidden", "email": None, "role": "standard-user", "name": "ten"},
        ]
    )

    result = manager.remove_from_team(
        api,
        "yun10@yunfei.life",
        return_status=True,
        lookup_retries=0,
        retry_interval=0,
    )

    assert result == "removed"
    assert api.deleted == ["user-hidden"]


def test_cmd_rotate_skips_google_accounts_during_auto_reuse(monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    count_values = iter([2, 3])
    events = []

    monkeypatch.setattr(config, "ROTATE_SKIP_REUSE", False)
    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: next(count_values))
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        manager,
        "get_standby_accounts",
        lambda: [
            {"email": "bubblehuntr@gmail.com"},
            {"email": "old-2@example.com"},
        ],
    )
    monkeypatch.setattr(
        manager,
        "reinvite_account",
        lambda _chatgpt, _mail, acc: events.append(("reinvite", acc["email"])) or True,
    )
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda _chatgpt, _mail: events.append(("create", None)) or True,
    )
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: events.append(("sync_to_cpa", None)))

    manager.cmd_rotate(target_seats=3)

    assert events == [
        ("sync_account_states", None),
        ("cmd_check", None),
        ("reinvite", "old-2@example.com"),
        ("sync_to_cpa", None),
    ]
    assert chatgpt.stopped == 1


def test_cmd_rotate_can_defer_final_sync_when_running_as_api_task(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: 3)
    monkeypatch.setattr(manager, "_count_pool_active_accounts", lambda *args, **kwargs: 2)
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda _chatgpt, _mail: (_ for _ in ()).throw(AssertionError("should not create when member count is full")),
    )
    monkeypatch.setattr(
        manager,
        "sync_to_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("final sync should be scheduled, not run inline")),
    )
    monkeypatch.setattr(
        manager,
        "_schedule_post_task_sync",
        lambda stage_label: events.append(("schedule_post_sync", stage_label)),
    )

    manager.cmd_rotate(target_seats=3, background_post_sync=True)

    assert events == [
        ("sync_account_states", None),
        ("cmd_check", None),
        ("schedule_post_sync", "[轮转]"),
    ]


def test_cmd_rotate_clears_pending_invite_when_pool_underfilled(monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    events = []
    count_values = iter([3, 2])

    monkeypatch.setattr(config, "ROTATE_SKIP_REUSE", False)
    monkeypatch.setattr(config, "ROTATE_ALLOW_NEW_ACCOUNTS", False)
    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda force_auth_repair=False: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: [{"email": "yun9@yunfei.life", "status": manager.STATUS_ACTIVE}])
    monkeypatch.setattr(manager, "_is_replaceable_pool_blocker", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: next(count_values))
    monkeypatch.setattr(manager, "_count_pool_active_accounts", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        manager,
        "_cancel_stale_pending_invites_for_capacity",
        lambda *_args, **_kwargs: events.append(("cancel_pending", None)) or ["yun12@yunfei.life"],
    )
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("new account creation is disabled")),
    )
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: events.append(("sync_to_cpa", None)))

    manager.cmd_rotate(target_seats=3)

    assert ("cancel_pending", None) in events
    assert events.index(("cancel_pending", None)) > events.index(("cmd_check", None))


def test_prepare_remote_capacity_counts_pending_invites(monkeypatch):
    class FakeApi:
        def __init__(self):
            self.browser = True
            self.deleted = []
            self.invite_present = True

        def start(self):
            self.browser = True

        def _api_fetch(self, method, path, body=None):
            if method == "GET" and path.endswith("/users"):
                return {
                    "status": 200,
                    "body": json.dumps(
                        {
                            "items": [
                                {"id": "owner", "email": "owner@example.com", "role": "account-owner"},
                                {"id": "u1", "email": None, "role": "standard-user"},
                            ]
                        }
                    ),
                }
            if method == "GET" and path.endswith("/invites"):
                items = [{"id": "inv1", "email_address": "pending@yunfei.life"}] if self.invite_present else []
                return {
                    "status": 200,
                    "body": json.dumps({"items": items}),
                }
            if method == "DELETE" and "/invites/" in path:
                self.deleted.append(path.rsplit("/", 1)[-1])
                self.invite_present = False
                return {"status": 204, "body": ""}
            return {"status": 404, "body": "{}"}

    fake = FakeApi()

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_configured_mail_domains", lambda: {"yunfei.life"})
    monkeypatch.setattr(manager, "_find_team_auth_file", lambda _email: None)
    monkeypatch.setattr(manager, "load_accounts", lambda: [])

    assert manager._prepare_remote_capacity_for_new_seat(fake) is True
    assert fake.deleted == ["inv1"]


def test_invite_to_team_refuses_when_pending_invite_still_occupies_capacity(monkeypatch):
    class FakeApi:
        def __init__(self):
            self.browser = True
            self.invite_calls = []

        def start(self):
            self.browser = True

        def _api_fetch(self, method, path, body=None):
            if method == "GET" and path.endswith("/users"):
                return {
                    "status": 200,
                    "body": json.dumps(
                        {
                            "items": [
                                {"id": "owner", "email": "owner@example.com", "role": "account-owner"},
                                {"id": "u1", "email": None, "role": "standard-user"},
                            ]
                        }
                    ),
                }
            if method == "GET" and path.endswith("/invites"):
                return {
                    "status": 200,
                    "body": json.dumps({"items": [{"id": "inv1", "email_address": "manual@example.com"}]}),
                }
            return {"status": 404, "body": "{}"}

        def invite_member(self, email, seat_type="default"):
            self.invite_calls.append((email, seat_type))
            return 200, {}

    fake = FakeApi()

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_configured_mail_domains", lambda: {"yunfei.life"})
    monkeypatch.setattr(manager, "load_accounts", lambda: [])

    assert manager.invite_to_team(fake, "yun13@yunfei.life") is False
    assert fake.invite_calls == []


def test_sync_account_states_matches_hidden_member_by_auth_user_id(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-yun9@yunfei.life-team.json"
    auth_file.write_text(
        json.dumps(
            {
                "email": "yun9@yunfei.life",
                "account_id": "acct-1",
                "access_token": _jwt_with_chatgpt_user_id("user-hidden-yun9"),
            }
        ),
        encoding="utf-8",
    )
    accounts = [
        {
            "email": "yun9@yunfei.life",
            "status": manager.STATUS_STANDBY,
            "auth_file": str(auth_file),
        }
    ]
    updates = []

    class FakeApi:
        browser = True

        def _api_fetch(self, method, path, body=None):
            if method == "GET" and path.endswith("/users"):
                return {
                    "status": 200,
                    "body": json.dumps(
                        {
                            "items": [
                                {"id": "owner", "email": "owner@example.com", "role": "account-owner"},
                                {"id": "user-hidden-yun9", "email": None, "role": "standard-user", "name": "yun"},
                            ]
                        }
                    ),
                }
            return {"status": 404, "body": "{}"}

    def fake_update(email, **fields):
        updates.append((email, fields))
        accounts[0].update(fields)

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts)
    monkeypatch.setattr(manager, "update_account", fake_update)
    monkeypatch.setattr(manager, "save_accounts", lambda _accounts: None)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda email: email == "owner@example.com")

    manager.sync_account_states(FakeApi())

    assert accounts[0]["status"] == manager.STATUS_ACTIVE
    assert accounts[0]["workspace_account_id"] == "acct-1"
    assert updates


def test_cancel_stale_pending_invite_allows_standby_account_with_auth_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-yun12@yunfei.life-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    class FakeApi:
        def __init__(self):
            self.invite_present = True
            self.deleted = []

        def _api_fetch(self, method, path, body=None):
            if method == "GET" and path.endswith("/users"):
                return {
                    "status": 200,
                    "body": json.dumps(
                        {
                            "items": [
                                {"id": "owner", "email": "owner@example.com", "role": "account-owner"},
                                {"id": "user-hidden", "email": None, "role": "standard-user"},
                            ]
                        }
                    ),
                }
            if method == "GET" and path.endswith("/invites"):
                items = [{"id": "inv-yun12", "email_address": "yun12@yunfei.life"}] if self.invite_present else []
                return {"status": 200, "body": json.dumps({"items": items})}
            if method == "DELETE" and "/invites/" in path:
                self.deleted.append(path.rsplit("/", 1)[-1])
                self.invite_present = False
                return {"status": 204, "body": ""}
            return {"status": 404, "body": "{}"}

    fake = FakeApi()

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct-1")
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "yun12@yunfei.life",
                "status": manager.STATUS_STANDBY,
                "auth_file": str(auth_file),
            }
        ],
    )
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "delete_managed_account", lambda *args, **kwargs: None)

    assert manager._cancel_stale_pending_invites_for_capacity(fake, stage_label="[test]") == ["yun12@yunfei.life"]
    assert fake.deleted == ["inv-yun12"]


def test_replaceable_pool_blocker_reason_reports_concrete_evidence(tmp_path):
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    assert (
        manager._replaceable_pool_blocker_reason(
            {"email": "missing@example.com", "status": manager.STATUS_ACTIVE, "auth_file": ""}
        )
        == "missing_auth"
    )
    assert (
        manager._replaceable_pool_blocker_reason(
            {"email": "invalid@example.com", "status": manager.STATUS_AUTH_INVALID}
        )
        == "auth_invalid"
    )
    assert (
        manager._replaceable_pool_blocker_reason(
            {"email": "exhausted@example.com", "status": manager.STATUS_EXHAUSTED}
        )
        == "quota_exhausted"
    )
    assert (
        manager._replaceable_pool_blocker_reason(
            {
                "email": "managed-protected@example.com",
                "status": manager.STATUS_ACTIVE,
                "auth_file": str(auth_file),
                "mail_account_id": 1,
                "auth_retry_paused": True,
                "protect_team_seat": True,
            }
        )
        == "auth_retry_paused"
    )
    assert (
        manager._replaceable_pool_blocker_reason(
            {
                "email": "manual-protected@example.com",
                "status": manager.STATUS_ACTIVE,
                "auth_file": str(auth_file),
                "auth_retry_paused": True,
                "protect_team_seat": True,
            }
        )
        is None
    )


def test_create_new_account_uses_domain_auto_join_before_invite(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setenv("ROTATE_NEW_ACCOUNT_MODE", "domain_auto_join_first")
    monkeypatch.setenv("AUTOTEAM_AUTO_JOIN_DOMAINS", "example.com")
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_check_pending_invites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        manager,
        "create_account_via_invite",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invite should not run for auto-join domain")),
    )
    monkeypatch.setattr(manager, "create_account_direct", lambda *_args, **_kwargs: events.append("direct") or "new@example.com")

    result = manager.create_new_account(chatgpt, _FakeMailClient())

    assert result == "new@example.com"
    assert events == ["direct"]
    assert chatgpt.stopped == 1


def test_create_new_account_invite_first_mode_preserves_invite_order(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setenv("ROTATE_NEW_ACCOUNT_MODE", "invite_first")
    monkeypatch.setattr(manager, "_check_pending_invites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manager, "create_account_via_invite", lambda *_args, **_kwargs: events.append("invite") or "new@example.com")
    monkeypatch.setattr(
        manager,
        "create_account_direct",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct should not run when invite succeeds")),
    )

    result = manager.create_new_account(chatgpt, _FakeMailClient())

    assert result == "new@example.com"
    assert events == ["invite"]


def test_create_new_account_domain_auto_join_falls_back_to_invite(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setenv("ROTATE_NEW_ACCOUNT_MODE", "domain_auto_join_first")
    monkeypatch.setenv("AUTOTEAM_AUTO_JOIN_DOMAINS", "example.com")
    monkeypatch.setenv("ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE", "true")
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_check_pending_invites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "create_account_direct", lambda *_args, **_kwargs: events.append("direct") or None)
    monkeypatch.setattr(manager, "create_account_via_invite", lambda *_args, **_kwargs: events.append("invite") or "new@example.com")

    result = manager.create_new_account(chatgpt, _FakeMailClient())

    assert result == "new@example.com"
    assert events == ["direct", "invite"]


def test_create_new_account_does_not_retry_direct_after_invite_fallback_failure(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setenv("ROTATE_NEW_ACCOUNT_MODE", "domain_auto_join_first")
    monkeypatch.setenv("AUTOTEAM_AUTO_JOIN_DOMAINS", "example.com")
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_check_pending_invites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "create_account_direct", lambda *_args, **_kwargs: events.append("direct") or None)
    monkeypatch.setattr(manager, "create_account_via_invite", lambda *_args, **_kwargs: events.append("invite") or None)

    assert manager.create_new_account(chatgpt, _FakeMailClient()) is None
    assert events == ["direct", "invite"]


def test_create_new_account_domain_auto_join_respects_allowlist(monkeypatch):
    chatgpt = _FakeChatGPT()
    events = []

    monkeypatch.setenv("ROTATE_NEW_ACCOUNT_MODE", "domain_auto_join_first")
    monkeypatch.setenv("AUTOTEAM_AUTO_JOIN_DOMAINS", "other.example")
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_check_pending_invites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        manager,
        "create_account_direct",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct should not run for unlisted domain")),
    )
    monkeypatch.setattr(manager, "create_account_via_invite", lambda *_args, **_kwargs: events.append("invite") or "new@example.com")

    result = manager.create_new_account(chatgpt, _FakeMailClient())

    assert result == "new@example.com"
    assert events == ["invite"]


def test_cmd_rotate_removes_replaceable_blocker_before_creating_replacement(monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    accounts = [{"email": "blocked@example.com", "status": manager.STATUS_ACTIVE, "auth_file": ""}]
    counts = iter([3, 2, 2, 3])
    events = []

    monkeypatch.setattr(config, "ROTATE_SKIP_REUSE", True)
    monkeypatch.setattr(config, "ROTATE_ALLOW_NEW_ACCOUNTS", True)
    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts)
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: next(counts))
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_a, **_kw: True)
    monkeypatch.setattr(manager, "_wait_for_remote_capacity_after_removal", lambda *_a, **_kw: (2, True))
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [])
    monkeypatch.setattr(manager, "time", manager.time)
    monkeypatch.setattr(manager.time, "sleep", lambda *_args, **_kwargs: None)

    def fake_update(email, **kwargs):
        events.append(("update", email, kwargs.get("status"), kwargs.get("_reason")))
        for acc in accounts:
            if acc["email"] == email:
                acc.update(kwargs)

    def fake_remove(_chatgpt, email, *, return_status=False, **_kwargs):
        events.append(("remove", email))
        return "removed" if return_status else True

    def fake_create(_chatgpt, _mail):
        events.append(("create", None))
        accounts.append(
            {
                "email": "new@example.com",
                "status": manager.STATUS_ACTIVE,
                "auth_file": "auth.json",
            }
        )
        return "new@example.com"

    monkeypatch.setattr(manager, "update_account", fake_update)
    monkeypatch.setattr(manager, "remove_from_team", fake_remove)
    monkeypatch.setattr(manager, "create_new_account", fake_create)
    monkeypatch.setattr(manager, "_validate_managed_account_operational", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: events.append(("sync_to_cpa", None)))

    manager.cmd_rotate(target_seats=3)

    assert events.index(("remove", "blocked@example.com")) < events.index(("create", None))
    assert ("update", "blocked@example.com", manager.STATUS_STANDBY, "missing_auth") in events


def test_cmd_rotate_target2_refills_after_exhausted_removal_despite_transient_overcount(tmp_path, monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    old_auth = tmp_path / "old.json"
    standby_auth = tmp_path / "standby.json"
    old_auth.write_text('{"access_token": "old-token"}', encoding="utf-8")
    standby_auth.write_text('{"access_token": "standby-token"}', encoding="utf-8")

    state = {
        "team_count": 2,
        "accounts": [
            {
                "email": "old@example.com",
                "status": manager.STATUS_EXHAUSTED,
                "auth_file": str(old_auth),
                "last_quota": {"primary_pct": 100, "primary_resets_at": 1_700_001_000},
            },
            {
                "email": "standby@example.com",
                "status": manager.STATUS_STANDBY,
                "auth_file": str(standby_auth),
                "last_quota": {"primary_pct": 10, "primary_resets_at": 1_700_000_000},
            },
        ],
    }
    counts = iter([2, 3, 2])
    events = []

    def fake_load_accounts():
        return [dict(acc) for acc in state["accounts"]]

    def fake_update(email, **kwargs):
        events.append(("update", email, kwargs.get("status"), kwargs.get("_reason")))
        for acc in state["accounts"]:
            if acc["email"] == email:
                acc.update(kwargs)
                return

    def fake_count(_chatgpt):
        count = next(counts)
        events.append(("count", count))
        return count

    def fake_wait(_chatgpt, **kwargs):
        events.append(("wait_capacity", kwargs["removed_email"], kwargs["target"]))
        return 3, False

    def fake_remove(_chatgpt, email, *, return_status=False, **_kwargs):
        events.append(("remove", email, return_status))
        state["team_count"] -= 1
        return "removed" if return_status else True

    def fake_quota(token, *args, **kwargs):
        events.append(("quota", token))
        if token == "old-token":
            return (
                "exhausted",
                {"quota_info": {"primary_pct": 100, "weekly_pct": 100}, "resets_at": 1_700_001_000},
            )
        return "ok", {"primary_pct": 10, "weekly_pct": 10}

    def fake_reinvite(_chatgpt, _mail, acc):
        events.append(("reinvite", acc["email"]))
        state["team_count"] += 1
        fake_update(acc["email"], status=manager.STATUS_ACTIVE, last_active_at=1_700_000_000)
        return True

    monkeypatch.setattr(config, "ROTATE_SKIP_REUSE", False)
    monkeypatch.setattr(manager, "sync_account_states", lambda: events.append(("sync_account_states", None)))
    monkeypatch.setattr(manager, "cmd_check", lambda: events.append(("cmd_check", None)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: chatgpt)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: _FakeMailClient())
    monkeypatch.setattr(manager, "load_accounts", fake_load_accounts)
    monkeypatch.setattr(manager, "update_account", fake_update)
    monkeypatch.setattr(manager, "get_team_occupancy_count", fake_count)
    monkeypatch.setattr(manager, "_prepare_remote_capacity_for_new_seat", lambda *_a, **_kw: True)
    monkeypatch.setattr(manager, "_wait_for_remote_capacity_after_removal", fake_wait)
    monkeypatch.setattr(
        manager,
        "get_standby_accounts",
        lambda: [dict(acc) for acc in state["accounts"] if acc["status"] == manager.STATUS_STANDBY],
    )
    monkeypatch.setattr(manager, "check_codex_quota", fake_quota)
    monkeypatch.setattr(manager, "reinvite_account", fake_reinvite)
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should reuse standby after removing exhausted blocker, not create before remove")
        ),
    )
    monkeypatch.setattr(manager, "remove_from_team", fake_remove)
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: events.append(("sync_to_cpa", None)))

    manager.cmd_rotate(target_seats=2)

    assert events.index(("remove", "old@example.com", True)) < events.index(("reinvite", "standby@example.com"))
    assert ("count", 3) in events
    assert events.count(("remove", "old@example.com", True)) == 1
    assert state["team_count"] == 2
    assert next(acc for acc in state["accounts"] if acc["email"] == "old@example.com")["status"] == manager.STATUS_STANDBY
    assert next(acc for acc in state["accounts"] if acc["email"] == "standby@example.com")[
        "status"
    ] == manager.STATUS_ACTIVE


def test_replace_single_reuses_standby_when_old_team_token_is_revoked_after_reset(tmp_path, monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    mail = _FakeMailClient()
    standby_auth = tmp_path / "standby.json"
    standby_auth.write_text('{"access_token": "revoked-old-team-token"}', encoding="utf-8")
    events = []

    standby = {
        "email": "standby@example.com",
        "status": manager.STATUS_STANDBY,
        "auth_file": str(standby_auth),
        "_quota_recovered": True,
        "last_quota": {
            "primary_pct": 10,
            "primary_resets_at": 1,
            "weekly_pct": 10,
            "weekly_resets_at": 1,
        },
    }

    monkeypatch.setattr(config, "ROTATE_ALLOW_NEW_ACCOUNTS", False)
    monkeypatch.setattr(manager, "remove_from_team", lambda *_a, **_kw: events.append(("remove", _a[1])) or "removed")
    monkeypatch.setattr(manager, "_wait_for_remote_capacity_after_removal", lambda *_a, **_kw: None)
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: events.append(("update", email, kw.get("status"))))
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: 2)
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [standby])
    monkeypatch.setattr(manager, "check_codex_quota", lambda token: ("auth_error", None))
    monkeypatch.setattr(
        manager,
        "reinvite_account",
        lambda _chatgpt, _mail, acc: events.append(("reinvite", acc["email"])) or True,
    )
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revoked old standby token should not force new-account fallback")
        ),
    )

    outcome = manager._replace_single(chatgpt, mail, "old@example.com", reason="unit")

    assert outcome == {"kicked": True, "filled_by": "standby@example.com", "method": "reuse", "error": None}
    assert ("reinvite", "standby@example.com") in events


def test_replace_single_does_not_reuse_revoked_standby_when_history_still_low(tmp_path, monkeypatch):
    import autoteam.config as config

    chatgpt = _FakeChatGPT()
    mail = _FakeMailClient()
    standby_auth = tmp_path / "standby.json"
    standby_auth.write_text('{"access_token": "revoked-old-team-token"}', encoding="utf-8")
    events = []

    standby = {
        "email": "standby@example.com",
        "status": manager.STATUS_STANDBY,
        "auth_file": str(standby_auth),
        "_quota_recovered": True,
        "last_quota": {
            "primary_pct": 99,
            "primary_resets_at": 9_999_999_999,
            "weekly_pct": 10,
            "weekly_resets_at": 9_999_999_999,
        },
    }

    monkeypatch.setattr(config, "ROTATE_ALLOW_NEW_ACCOUNTS", False)
    monkeypatch.setattr(manager, "remove_from_team", lambda *_a, **_kw: events.append(("remove", _a[1])) or "removed")
    monkeypatch.setattr(manager, "_wait_for_remote_capacity_after_removal", lambda *_a, **_kw: None)
    monkeypatch.setattr(manager, "update_account", lambda email, **kw: events.append(("update", email, kw.get("status"))))
    monkeypatch.setattr(manager, "get_team_occupancy_count", lambda _chatgpt: 2)
    monkeypatch.setattr(manager, "get_standby_accounts", lambda: [standby])
    monkeypatch.setattr(manager, "check_codex_quota", lambda token: ("auth_error", None))
    monkeypatch.setattr(
        manager,
        "reinvite_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("low historical quota should not be reinvited")
        ),
    )

    outcome = manager._replace_single(chatgpt, mail, "old@example.com", reason="unit")

    assert outcome == {
        "kicked": True,
        "filled_by": None,
        "method": None,
        "error": "new_account_creation_disabled",
    }
