import json

from autoteam import local_codex_sync


def test_write_local_codex_auth_converts_bundle_to_cli_auth(tmp_path):
    src = tmp_path / "codex-user@example.com-team.json"
    dest = tmp_path / "codex" / "auth.json"
    src.write_text(
        json.dumps(
            {
                "id_token": "id",
                "access_token": "access",
                "refresh_token": "refresh",
                "account_id": "acc-1",
                "last_refresh": 123,
            }
        ),
        encoding="utf-8",
    )

    result = local_codex_sync.write_local_codex_auth(src, dest=dest)

    assert result["ok"] is True
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload == {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id",
            "access_token": "access",
            "refresh_token": "refresh",
            "account_id": "acc-1",
        },
        "last_refresh": 123,
    }


def test_select_best_active_account_prefers_lowest_primary_pct(monkeypatch, tmp_path):
    high = tmp_path / "high.json"
    low = tmp_path / "low.json"
    high.write_text("{}", encoding="utf-8")
    low.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.local_codex_sync.load_accounts",
        lambda: [
            {
                "email": "high@example.com",
                "status": "active",
                "auth_file": str(high),
                "last_quota": {"primary_pct": 70},
            },
            {
                "email": "low@example.com",
                "status": "active",
                "auth_file": str(low),
                "last_quota": {"primary_pct": 5},
            },
        ],
    )

    assert local_codex_sync.select_best_active_account()["email"] == "low@example.com"
