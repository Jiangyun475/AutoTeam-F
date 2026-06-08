from pathlib import Path

from autoteam import sync_targets


def test_get_sync_target_states_uses_implicit_config_presence():
    env = {
        "CPA_URL": "http://127.0.0.1:8317",
        "CPA_KEY": "key-1",
        "SUB2API_URL": "http://sub2api.local",
        "SUB2API_EMAIL": "admin@example.com",
        "SUB2API_PASSWORD": "secret",
    }

    assert sync_targets.get_sync_target_states(env) == {
        "cpa": True,
        "sub2api": True,
        "local_codex": False,
    }


def test_get_sync_target_states_respects_explicit_toggle_override():
    env = {
        "SYNC_TARGET_CPA": "false",
        "CPA_URL": "http://127.0.0.1:8317",
        "CPA_KEY": "key-1",
        "SYNC_TARGET_SUB2API": "true",
    }

    assert sync_targets.get_sync_target_states(env) == {
        "cpa": False,
        "sub2api": True,
        "local_codex": False,
    }


def test_get_sync_target_states_enables_local_codex_only_when_explicit():
    assert sync_targets.get_sync_target_states({})["local_codex"] is False
    assert sync_targets.get_sync_target_states({"SYNC_TARGET_LOCAL_CODEX": "true"})["local_codex"] is True


def test_describe_sync_targets_formats_labels():
    assert sync_targets.describe_sync_targets(["cpa"]) == "CPA"
    assert sync_targets.describe_sync_targets(["cpa", "sub2api"]) == "CPA + Sub2API"
    assert sync_targets.describe_sync_targets(["local_codex"]) == "本机 Codex CLI"


def test_sync_account_to_configured_targets_uploads_one_active_auth(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-user@example.com-team-a.json"
    auth_file.write_text('{"access_token":"token"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "user@example.com",
                "status": "active",
                "auth_file": str(auth_file),
                "disabled": False,
                "last_quota": {"primary_pct": 12},
            }
        ],
    )
    monkeypatch.setattr(
        sync_targets,
        "get_enabled_sync_targets",
        lambda: [sync_targets.SYNC_TARGET_CPA, sync_targets.SYNC_TARGET_SUB2API],
    )

    cpa_uploads = []
    sub2_uploads = []
    monkeypatch.setattr("autoteam.cpa_sync.upload_to_cpa", lambda path: cpa_uploads.append(Path(path).name) or True)
    monkeypatch.setattr(
        "autoteam.sub2api_sync.sync_account_to_sub2api",
        lambda email, filepath, *, quota_info=None: sub2_uploads.append(
            (email, Path(filepath).name, quota_info)
        )
        or {"uploaded": f"sub2api-{Path(filepath).name}", "action": "created", "account_id": 12},
    )

    result = sync_targets.sync_account_to_configured_targets("USER@example.com", str(auth_file))

    assert result["ok"] is True
    assert cpa_uploads == [auth_file.name]
    assert sub2_uploads == [("user@example.com", auth_file.name, {"primary_pct": 12})]
    assert result["targets"]["cpa"]["uploaded"] == auth_file.name
    assert result["targets"]["sub2api"]["uploaded"] == f"sub2api-{auth_file.name}"


def test_sync_account_to_configured_targets_skips_non_active_auth(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-user@example.com-team-a.json"
    auth_file.write_text('{"access_token":"token"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "user@example.com", "status": "standby", "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr(
        sync_targets,
        "get_enabled_sync_targets",
        lambda: [sync_targets.SYNC_TARGET_CPA, sync_targets.SYNC_TARGET_SUB2API],
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.upload_to_cpa",
        lambda _path: (_ for _ in ()).throw(AssertionError("inactive account must not upload to CPA")),
    )
    monkeypatch.setattr(
        "autoteam.sub2api_sync.sync_account_to_sub2api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inactive account must not upload to Sub2API")
        ),
    )

    result = sync_targets.sync_account_to_configured_targets("user@example.com", str(auth_file))

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["reason"] == "account_not_active"


def test_sync_account_to_configured_targets_skips_degraded_grace_auth(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-grace@example.com-team-a.json"
    auth_file.write_text('{"access_token":"token"}', encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "grace@example.com", "status": "degraded_grace", "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr(
        sync_targets,
        "get_enabled_sync_targets",
        lambda: [sync_targets.SYNC_TARGET_CPA, sync_targets.SYNC_TARGET_SUB2API],
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.upload_to_cpa",
        lambda _path: (_ for _ in ()).throw(AssertionError("grace account must not upload to CPA")),
    )
    monkeypatch.setattr(
        "autoteam.sub2api_sync.sync_account_to_sub2api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("grace account must not upload to Sub2API")
        ),
    )

    result = sync_targets.sync_account_to_configured_targets("grace@example.com", str(auth_file))

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["reason"] == "account_not_active"
    assert result["status"] == "degraded_grace"
