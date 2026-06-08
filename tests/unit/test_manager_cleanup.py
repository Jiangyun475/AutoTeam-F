import json

import autoteam.manager as manager


class _FakeChatGPT:
    def __init__(self):
        self.deleted_invites = []
        self.deleted_users = []

    def start(self):
        return None

    def stop(self):
        return None

    def _api_fetch(self, method, path):
        if method == "GET" and path.endswith("/users"):
            return {
                "status": 200,
                "body": json.dumps(
                    {
                        "items": [
                            {"email": None, "role": "account-owner", "id": "owner"},
                            {"email": "active@example.com", "role": "member", "id": "u1"},
                            {"email": "other@example.com", "role": "member", "id": "u2"},
                        ]
                    }
                ),
            }
        if method == "GET" and path.endswith("/invites"):
            return {
                "status": 200,
                "body": json.dumps(
                    {
                        "invites": [
                            {"email_address": "pending@example.com", "id": "inv1"},
                        ]
                    }
                ),
            }
        if method == "DELETE" and "/invites/" in path:
            self.deleted_invites.append(path.rsplit("/", 1)[-1])
            return {"status": 204, "body": ""}
        if method == "DELETE" and "/users/" in path:
            self.deleted_users.append(path.rsplit("/", 1)[-1])
            return {"status": 204, "body": ""}
        return {"status": 404, "body": "{}"}


def test_cmd_cleanup_handles_null_member_email_and_cancels_invite(monkeypatch):
    fake = _FakeChatGPT()

    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acct")
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake)
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {"email": "active@example.com", "status": manager.STATUS_ACTIVE},
            {"email": "pending@example.com", "status": manager.STATUS_PENDING},
        ],
    )
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "sync_to_cpa", lambda: None)

    manager.cmd_cleanup(max_seats=3)

    assert fake.deleted_invites == ["inv1"]
    assert fake.deleted_users == []
