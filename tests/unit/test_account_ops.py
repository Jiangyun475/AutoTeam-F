import threading

import pytest

from autoteam import account_ops


class _FakeChatGPT:
    def __init__(self, responses):
        self._responses = responses

    def _api_fetch(self, method, path, body=None):
        return self._responses[path]


def test_fetch_team_state_parses_members_and_invites(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 200,
                "body": '{"items":[{"email":"member@example.com"}]}',
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[{"email":"invite@example.com"}]}',
            },
        }
    )

    members, invites = account_ops.fetch_team_state(chatgpt)

    assert members == [{"email": "member@example.com"}]
    assert invites == [{"email": "invite@example.com"}]


def test_fetch_team_state_parses_nested_member_and_invite_shapes(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 200,
                "body": (
                    '{"items":[{"user":{"email":"Owner@Example.com","id":"user-1",'
                    '"account_role":"owner"}}],"total":1}'
                ),
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"items":[{"email_address":"Invite@Example.com","id":"inv-1"}],"total":1}',
            },
        }
    )

    members, invites = account_ops.fetch_team_state(chatgpt)

    assert account_ops.team_member_email(members[0]) == "owner@example.com"
    assert account_ops.team_member_user_id(members[0]) == "user-1"
    assert account_ops.team_member_role(members[0]) == "owner"
    assert account_ops.team_invite_email(invites[0]) == "invite@example.com"


def test_delete_team_invite_falls_back_to_collection_delete():
    calls = []

    class _InviteChatGPT:
        def _api_fetch(self, method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("/invites/inv-1"):
                return {"status": 405, "body": '{"detail":"Method Not Allowed"}'}
            if path.endswith("/invites") and method == "DELETE":
                return {"status": 200, "body": '{"success":true}'}
            raise AssertionError(f"unexpected api call: {method} {path} {body}")

    result = account_ops.delete_team_invite(
        _InviteChatGPT(),
        "acc-1",
        {"id": "inv-1", "email_address": "invite@example.com"},
    )

    assert result["status"] == 200
    assert calls == [
        ("DELETE", "/backend-api/accounts/acc-1/invites/inv-1", None),
        ("DELETE", "/backend-api/accounts/acc-1/invites", {"email_address": "invite@example.com"}),
    ]


def test_fetch_team_state_raises_readable_error_when_users_response_is_html(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 200,
                "body": "<!doctype html><html><body>login</body></html>",
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="Team 成员接口返回了非 JSON 内容"):
        account_ops.fetch_team_state(chatgpt)


def test_fetch_team_state_raises_readable_error_when_users_auth_fails(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 403,
                "body": '{"detail":"forbidden"}',
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="请重新完成管理员登录"):
        account_ops.fetch_team_state(chatgpt)


def test_delete_managed_account_uses_generic_mail_provider_fields(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": str(auth_file),
            "mail_provider": "cloudflare_temp_email",
            "mail_account_id": 55,
            "cloudmail_account_id": None,
        }
    ]
    deleted = []
    remote_calls = []

    class _FakeMailClient:
        provider_name = "cloudflare_temp_email"

        def delete_account(self, account_id):
            deleted.append(account_id)
            return {"code": 200}

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(
        account_ops,
        "delete_account_from_configured_targets",
        lambda *args, **kwargs: remote_calls.append((args, kwargs)) or {"sub2api": {"deleted": ["sub-id"]}},
    )
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        remove_remote=False,
        mail_client=_FakeMailClient(),
        sync_cpa_after=False,
    )

    assert deleted == [55]
    assert remote_calls == [(("user@example.com",), {"auth_names": ["codex-user@example.com-team.json"], "include_disabled": True})]
    assert cleanup["local_record"] is True
    assert cleanup["cloudmail_deleted"] is True
    assert cleanup["sub2api_accounts"] == ["sub-id"]


def test_delete_managed_account_preserves_local_credential_seat(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-manual@example.com-team.json"
    auth_file.write_text('{"email":"manual@example.com"}', encoding="utf-8")

    accounts = [
        {
            "email": "manual@example.com",
            "status": "active",
            "auth_file": str(auth_file),
            "mail_account_id": None,
            "cloudmail_account_id": None,
        }
    ]

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(
        account_ops,
        "save_accounts",
        lambda _items: (_ for _ in ()).throw(AssertionError("protected account must not be deleted")),
    )
    monkeypatch.setattr(
        account_ops,
        "delete_account_from_configured_targets",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("protected account must not sync-delete")),
    )

    cleanup = account_ops.delete_managed_account("manual@example.com", remove_remote=False, sync_cpa_after=False)

    assert cleanup["protected_local_credential"] is True
    assert cleanup["local_record"] is False
    assert auth_file.exists()


def test_get_team_members_api_uses_normalized_team_shape_helpers(monkeypatch):
    from autoteam import api

    instances = []

    class FakeChatGPTTeamAPI:
        def __init__(self):
            self.stopped = False
            instances.append(self)

        def start(self):
            return None

        def stop(self):
            self.stopped = True

        def _api_fetch(self, method, path, body=None):
            responses = {
                "/backend-api/accounts/acc-1/users": {
                    "status": 200,
                    "body": (
                        '{"items":[{"user":{"email":"Member@Example.com","id":"user-1",'
                        '"account_role":"admin"}}]}'
                    ),
                },
                "/backend-api/accounts/acc-1/invites": {
                    "status": 200,
                    "body": '{"items":[{"email_address":"Invite@Example.com","id":"inv-1","role":"member"}]}',
                },
            }
            return responses[path]

    monkeypatch.setattr(api, "_playwright_lock", threading.Lock())
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr("autoteam.admin_state.get_admin_session_token", lambda: "session")
    monkeypatch.setattr("autoteam.admin_state.get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", FakeChatGPTTeamAPI)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [{"email": "member@example.com"}])

    result = api.get_team_members()

    assert result == {
        "members": [
            {
                "email": "member@example.com",
                "role": "admin",
                "user_id": "user-1",
                "is_local": True,
                "matched_by_user_id": False,
                "type": "member",
            },
            {
                "email": "invite@example.com",
                "role": "member",
                "user_id": "inv-1",
                "is_local": False,
                "type": "invite",
            },
        ],
        "total": 1,
        "invites": 1,
    }
    assert len(instances) == 1
    assert instances[0].stopped is True
