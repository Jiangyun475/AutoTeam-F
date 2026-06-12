import time

from autoteam import accounts


def test_add_and_update_account_persists_data(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "")

    accounts.add_account("user@example.com", "secret", cloudmail_account_id=123)
    created = accounts.load_accounts()

    assert len(created) == 1
    assert created[0]["email"] == "user@example.com"
    assert created[0]["cloudmail_account_id"] == 123
    assert created[0]["status"] == accounts.STATUS_PENDING
    assert created[0]["disabled"] is False

    updated = accounts.update_account("user@example.com", status=accounts.STATUS_ACTIVE, auth_file="auth.json")

    assert updated["status"] == accounts.STATUS_ACTIVE
    assert updated["auth_file"] == "auth.json"
    assert accounts.load_accounts()[0]["auth_file"] == "auth.json"


def test_get_active_accounts_excludes_main_account(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "owner@example.com", "status": accounts.STATUS_ACTIVE},
            {"email": "member@example.com", "status": accounts.STATUS_ACTIVE},
            {"email": "disabled@example.com", "status": accounts.STATUS_ACTIVE, "disabled": True},
            {"email": "standby@example.com", "status": accounts.STATUS_STANDBY},
        ]
    )

    active = accounts.get_active_accounts()

    assert [item["email"] for item in active] == ["member@example.com"]


def test_get_standby_accounts_orders_recovered_first_and_skips_main_account(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")

    now = time.time()
    accounts.save_accounts(
        [
            {
                "email": "owner@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": None,
                "quota_exhausted_at": None,
            },
            {
                "email": "ready@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 120,
            },
            {
                "email": "later@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now + 600,
                "quota_exhausted_at": now - 30,
            },
            {
                "email": "always@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": None,
                "quota_exhausted_at": None,
            },
            {
                "email": "disabled@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": None,
                "quota_exhausted_at": None,
                "disabled": True,
            },
        ]
    )

    standby = accounts.get_standby_accounts()

    assert [item["email"] for item in standby] == [
        "always@example.com",
        "ready@example.com",
        "later@example.com",
    ]
    assert standby[0]["_quota_recovered"] is True
    assert standby[1]["_quota_recovered"] is True
    assert standby[2]["_quota_recovered"] is False
    assert accounts.get_next_reusable_account()["email"] == "always@example.com"


def test_get_standby_accounts_prefers_higher_weekly_within_recovered(tmp_path, monkeypatch):
    """已恢复组内按有效周剩余降序摊开周预算;同档按 FIFO;5h 剩余不参与排序。

    周用量在重置前只增不减,快照里的周剩余是当前值的上界;周重置时间已过的
    快照按满额(100)处理。无快照视为满额,保持旧 FIFO 行为。
    """
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")

    now = time.time()
    accounts.save_accounts(
        [
            {
                "email": "low-weekly@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 300,
                "last_quota": {"primary_pct": 0, "weekly_pct": 80},
            },
            {
                "email": "high-weekly-low-primary@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 200,
                "last_quota": {"primary_pct": 70, "weekly_pct": 20},
            },
            {
                "email": "high-weekly-high-primary@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 100,
                "last_quota": {"primary_pct": 10, "weekly_pct": 20},
            },
            {
                "email": "unknown-quota@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 50,
            },
            {
                "email": "weekly-reset-passed@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "quota_exhausted_at": now - 40,
                "last_quota": {
                    "primary_pct": 0,
                    "weekly_pct": 90,
                    "weekly_resets_at": now - 10,
                },
            },
        ]
    )

    standby = accounts.get_standby_accounts()

    assert [item["email"] for item in standby] == [
        # 周剩余 100(无快照)与 100(周窗已重置)同档,按 quota_exhausted_at FIFO
        "unknown-quota@example.com",
        "weekly-reset-passed@example.com",
        # 周剩余 80 两个,FIFO:先离队的在前;5h 剩余(30% vs 90%)不影响顺序
        "high-weekly-low-primary@example.com",
        "high-weekly-high-primary@example.com",
        # 周剩余 20 殿后,即使 5h 是满的
        "low-weekly@example.com",
    ]


def test_get_standby_accounts_keeps_exhausted_waiting_after_recovered_even_with_better_quota(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")

    now = time.time()
    accounts.save_accounts(
        [
            {
                "email": "ready-low-weekly@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now - 60,
                "last_quota": {"primary_pct": 90, "weekly_pct": 95},
            },
            {
                "email": "waiting-high-weekly@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now + 600,
                "last_quota": {"primary_pct": 100, "primary_resets_at": now + 600, "weekly_pct": 0},
            },
        ]
    )

    standby = accounts.get_standby_accounts()

    assert [item["email"] for item in standby] == [
        "ready-low-weekly@example.com",
        "waiting-high-weekly@example.com",
    ]
    assert standby[0]["_quota_recovered"] is True
    assert standby[1]["_quota_recovered"] is False


def test_get_standby_accounts_does_not_let_stale_quota_unlock_cooldown(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")

    now = time.time()
    accounts.save_accounts(
        [
            {
                "email": "stale-cooldown@example.com",
                "status": accounts.STATUS_STANDBY,
                "quota_resets_at": now + 600,
                "last_quota": {
                    "primary_pct": 1,
                    "primary_resets_at": now + 600,
                    "weekly_pct": 37,
                    "weekly_resets_at": now + 3600,
                },
            }
        ]
    )

    standby = accounts.get_standby_accounts()

    assert standby[0]["email"] == "stale-cooldown@example.com"
    assert standby[0]["_quota_recovered"] is False


def test_load_accounts_normalizes_disabled_field(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)

    accounts_file.write_text(
        '[{"email":"legacy@example.com","status":"standby"},{"email":"off@example.com","status":"active","disabled":1}]',
        encoding="utf-8",
    )

    loaded = accounts.load_accounts()

    assert loaded[0]["disabled"] is False
    assert loaded[1]["disabled"] is True
