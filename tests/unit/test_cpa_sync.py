from pathlib import Path

import pytest

from autoteam import cpa_sync
from autoteam import mail as mail_module


def test_list_cpa_files_raises_on_non_200(monkeypatch):
    class _Resp:
        status_code = 503
        text = "service unavailable"

        def json(self):
            raise AssertionError("json() should not be called for non-200 responses")

    monkeypatch.setattr(cpa_sync.requests, "get", lambda *_args, **_kwargs: _Resp())

    with pytest.raises(RuntimeError, match="auth-files list failed"):
        cpa_sync.list_cpa_files()


def test_list_cpa_files_raises_on_non_json(monkeypatch):
    class _Resp:
        status_code = 200
        text = "<html>not json</html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(cpa_sync.requests, "get", lambda *_args, **_kwargs: _Resp())

    with pytest.raises(RuntimeError, match="returned non-JSON"):
        cpa_sync.list_cpa_files()


def test_infer_mail_provider_from_email_uses_matching_domain(monkeypatch):
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "mail.example.com")
    monkeypatch.setenv("MAILLAB_DOMAIN", "lab.example.com")
    monkeypatch.setenv("ADDY_IO_DOMAIN", "alias.example.com")

    assert mail_module.infer_mail_provider_from_email("user@mail.example.com") == "cf_temp_email"
    assert mail_module.infer_mail_provider_from_email("user@lab.example.com") == "maillab"
    assert mail_module.infer_mail_provider_from_email("user@alias.example.com") == "addy_io"
    assert mail_module.infer_mail_provider_from_email("user@unknown.example.com") == ""


def test_sync_to_cpa_skips_disabled_accounts_and_keeps_remote_when_active_pool_is_short(monkeypatch, tmp_path):
    enabled_auth = tmp_path / "codex-enabled@example.com-team-a.json"
    disabled_auth = tmp_path / "codex-disabled@example.com-team-b.json"
    enabled_auth.write_text('{"access_token":"token-enabled"}', encoding="utf-8")
    disabled_auth.write_text('{"access_token":"token-disabled"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "enabled@example.com",
                "status": "active",
                "auth_file": str(enabled_auth),
                "disabled": False,
            },
            {
                "email": "disabled@example.com",
                "status": "active",
                "auth_file": str(disabled_auth),
                "disabled": True,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": enabled_auth.name, "email": "enabled@example.com"},
            {"name": disabled_auth.name, "email": "disabled@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [enabled_auth.name]
    assert deleted == []
    assert result["disabled_skipped"] == 1
    assert result["delete_guard"]["allow_remote_delete"] is False
    assert result["delete_guard"]["skipped_remote_delete"] == 1


def test_sync_to_cpa_does_not_upload_degraded_grace_accounts(monkeypatch, tmp_path):
    first_auth = tmp_path / "codex-first@example.com-team-a.json"
    second_auth = tmp_path / "codex-second@example.com-team-b.json"
    grace_auth = tmp_path / "codex-grace@example.com-team-c.json"
    first_auth.write_text('{"access_token":"token-first"}', encoding="utf-8")
    second_auth.write_text('{"access_token":"token-second"}', encoding="utf-8")
    grace_auth.write_text('{"access_token":"token-grace"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "first@example.com",
                "status": "active",
                "auth_file": str(first_auth),
                "disabled": False,
            },
            {
                "email": "second@example.com",
                "status": "active",
                "auth_file": str(second_auth),
                "disabled": False,
            },
            {
                "email": "grace@example.com",
                "status": "degraded_grace",
                "auth_file": str(grace_auth),
                "disabled": False,
                "mail_provider": "cf_temp_email",
                "mail_account_id": "mail-grace",
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": first_auth.name, "email": "first@example.com"},
            {"name": second_auth.name, "email": "second@example.com"},
            {"name": grace_auth.name, "email": "grace@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [first_auth.name, second_auth.name]
    assert grace_auth.name not in uploaded
    assert deleted == [grace_auth.name]
    assert result["synced_active"] == 2
    assert result["delete_guard"]["allow_remote_delete"] is True


def test_sync_to_cpa_allows_remote_delete_when_active_pool_is_stable(monkeypatch, tmp_path):
    first_auth = tmp_path / "codex-first@example.com-team-a.json"
    second_auth = tmp_path / "codex-second@example.com-team-b.json"
    stale_auth = tmp_path / "codex-stale@example.com-team-c.json"
    first_auth.write_text('{"access_token":"token-first"}', encoding="utf-8")
    second_auth.write_text('{"access_token":"token-second"}', encoding="utf-8")
    stale_auth.write_text('{"access_token":"token-stale"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "first@example.com",
                "status": "active",
                "auth_file": str(first_auth),
                "disabled": False,
            },
            {
                "email": "second@example.com",
                "status": "active",
                "auth_file": str(second_auth),
                "disabled": False,
            },
            {
                "email": "stale@example.com",
                "status": "standby",
                "auth_file": "",
                "disabled": False,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": first_auth.name, "email": "first@example.com"},
            {"name": second_auth.name, "email": "second@example.com"},
            {"name": stale_auth.name, "email": "stale@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [first_auth.name, second_auth.name]
    assert deleted == [stale_auth.name]
    assert result["delete_guard"]["allow_remote_delete"] is True
    assert result["delete_guard"]["skipped_remote_delete"] == 0


def test_sync_to_cpa_deletes_main_and_personal_credentials(monkeypatch, tmp_path):
    first_auth = tmp_path / "codex-first@example.com-team-a.json"
    second_auth = tmp_path / "codex-second@example.com-team-b.json"
    personal_auth = tmp_path / "codex-personal@example.com-free-c.json"
    main_auth = tmp_path / "codex-main-owner.json"
    first_auth.write_text('{"access_token":"token-first"}', encoding="utf-8")
    second_auth.write_text('{"access_token":"token-second"}', encoding="utf-8")
    personal_auth.write_text('{"access_token":"token-personal"}', encoding="utf-8")
    main_auth.write_text('{"access_token":"token-main"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "first@example.com",
                "status": "active",
                "auth_file": str(first_auth),
                "disabled": False,
            },
            {
                "email": "second@example.com",
                "status": "active",
                "auth_file": str(second_auth),
                "disabled": False,
            },
            {
                "email": "personal@example.com",
                "status": "personal",
                "auth_file": str(personal_auth),
                "disabled": False,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": first_auth.name, "email": "first@example.com"},
            {"name": second_auth.name, "email": "second@example.com"},
            {"name": personal_auth.name, "email": "personal@example.com"},
            {"name": main_auth.name, "email": ""},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [first_auth.name, second_auth.name]
    assert deleted == [personal_auth.name, main_auth.name]
    assert result["synced_active"] == 2
    assert result["synced_personal"] == 0


def test_sync_to_cpa_deletes_non_active_credentials_when_remote_delete_is_allowed(monkeypatch, tmp_path):
    active_auth = tmp_path / "codex-active@example.com-team-a.json"
    second_active_auth = tmp_path / "codex-second-active@example.com-team-c.json"
    protected_auth = tmp_path / "codex-protected@example.com-team-b.json"
    active_auth.write_text('{"access_token":"token-active"}', encoding="utf-8")
    second_active_auth.write_text('{"access_token":"token-second-active"}', encoding="utf-8")
    protected_auth.write_text('{"access_token":"token-protected"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "active@example.com",
                "status": "active",
                "auth_file": str(active_auth),
                "disabled": False,
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": 1,
                "cloudmail_account_id": None,
            },
            {
                "email": "second-active@example.com",
                "status": "active",
                "auth_file": str(second_active_auth),
                "disabled": False,
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": 2,
                "cloudmail_account_id": None,
            },
            {
                "email": "protected@example.com",
                "status": "auth_invalid",
                "auth_file": str(protected_auth),
                "disabled": False,
                "mail_provider": "",
                "mail_account_id": None,
                "cloudmail_account_id": None,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": active_auth.name, "email": "active@example.com"},
            {"name": second_active_auth.name, "email": "second-active@example.com"},
            {"name": protected_auth.name, "email": "protected@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [active_auth.name, second_active_auth.name]
    assert deleted == [protected_auth.name]
    assert result["delete_guard"]["allow_remote_delete"] is True
    assert result["delete_guard"]["skipped_protected"] == 0


def test_sync_to_cpa_skips_exhausted_active_credential_before_upload(monkeypatch, tmp_path):
    first_auth = tmp_path / "codex-first@example.com-team-a.json"
    second_auth = tmp_path / "codex-second@example.com-team-b.json"
    exhausted_auth = tmp_path / "codex-exhausted@example.com-team-c.json"
    first_auth.write_text('{"email":"first@example.com","access_token":"token-first"}', encoding="utf-8")
    second_auth.write_text('{"email":"second@example.com","access_token":"token-second"}', encoding="utf-8")
    exhausted_auth.write_text('{"email":"exhausted@example.com","access_token":"token-exhausted"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "first@example.com",
                "status": "active",
                "auth_file": str(first_auth),
                "disabled": False,
                "mail_provider": "cf_temp_email",
                "mail_account_id": "mail-1",
            },
            {
                "email": "second@example.com",
                "status": "active",
                "auth_file": str(second_auth),
                "disabled": False,
                "mail_provider": "cf_temp_email",
                "mail_account_id": "mail-2",
            },
            {
                "email": "exhausted@example.com",
                "status": "active",
                "auth_file": str(exhausted_auth),
                "disabled": False,
                "mail_provider": "cf_temp_email",
                "mail_account_id": "mail-3",
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": first_auth.name, "email": "first@example.com"},
            {"name": second_auth.name, "email": "second@example.com"},
            {"name": exhausted_auth.name, "email": "exhausted@example.com"},
        ],
    )

    def fake_quota(token, **_kwargs):
        if token == "token-exhausted":
            return "exhausted", {"primary_pct": 0}
        return "ok", {"primary_pct": 50}

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_quota)
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == [first_auth.name, second_auth.name]
    assert deleted == [exhausted_auth.name]
    assert result["synced_active"] == 2
    assert result["active_publish"]["delete_remote"] == 1
    assert result["delete_guard"]["allow_remote_delete"] is True


def test_sync_to_cpa_keeps_remote_on_active_quota_network_error(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-active@example.com-team-a.json"
    auth_file.write_text('{"email":"active@example.com","access_token":"token-active"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "active@example.com", "status": "active", "auth_file": str(auth_file), "disabled": False}],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(cpa_sync, "list_cpa_files", lambda: [{"name": auth_file.name, "email": "active@example.com"}])

    uploaded = []
    deleted = []
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("network_error", {}))
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    result = cpa_sync.sync_to_cpa()

    assert uploaded == []
    assert deleted == []
    assert result["synced_active"] == 0
    assert result["active_publish"]["kept_remote"] == 1
    assert result["delete_guard"]["allow_remote_delete"] is False


def test_sync_to_cpa_refreshes_proxy_url_before_upload(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-enabled@example.com-team-a.json"
    auth_file.write_text('{"email":"enabled@example.com","access_token":"token-enabled"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "enabled@example.com",
                "status": "active",
                "auth_file": str(auth_file),
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(cpa_sync, "list_cpa_files", lambda: [])
    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", lambda *_args, **_kwargs: ("ok", {}))
    monkeypatch.setattr(
        "autoteam.ipv6_pool.ipv6_pool.ensure",
        lambda _email: "socks5://proxy.example:30000",
    )

    uploaded = []
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).read_text()) or True)

    result = cpa_sync.sync_to_cpa()

    assert result["uploaded"] == 1
    assert '"proxy_url": "socks5://proxy.example:30000"' in uploaded[0]
