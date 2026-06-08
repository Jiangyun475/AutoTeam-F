"""测试 cf_temp_email provider。

注:历史 cloud-mail API(`/account/list` `/email/list` `/email/latest`)在 cloudmail.py
切到 dreamhunter2333/cloudflare_temp_email 时已经不再适用,这些用例改为针对当前
`/admin/*` 路由的实现。OTP 提取测试与后端无关,沿用原断言。
"""

from autoteam import accounts, cloudflare_temp_email
from autoteam.mail import cf_temp_email


def test_search_emails_by_recipient_returns_normalized_records(monkeypatch):
    client = cf_temp_email.CfTempEmailClient()
    target = "tmp-user@example.com"

    monkeypatch.setattr(accounts, "load_accounts", lambda: [])

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    raw_mime = (
        "From: noreply@tm.openai.com\r\n"
        f"To: {target}\r\n"
        "Subject: Your ChatGPT code is 189799\r\n"
        "Message-ID: <abc@x>\r\n"
        "\r\n"
        "Your ChatGPT code is 189799\r\n"
    )

    def fake_get(path, params=None):
        assert path == "/admin/mails"
        assert (params or {}).get("address") == target
        return _Resp(
            {
                "results": [
                    {
                        "id": 15,
                        "address": target,
                        "raw": raw_mime,
                        "source": "noreply@tm.openai.com",
                        "created_at": 1761331200,
                    }
                ]
            }
        )

    monkeypatch.setattr(client, "_admin_get", fake_get)

    emails = client.search_emails_by_recipient(target, size=5)

    assert len(emails) == 1
    assert emails[0]["emailId"] == 15
    assert emails[0]["subject"] == "Your ChatGPT code is 189799"
    assert emails[0]["sendEmail"] == "noreply@tm.openai.com"
    assert emails[0]["accountEmail"] == target
    assert emails[0]["createTime"] == 1761331200


def test_cloudflare_temp_email_top_level_compat_uses_provider(monkeypatch):
    assert (
        cloudflare_temp_email.normalize_cloudflare_temp_email_base_url("https://mail.example.com/admin/")
        == "https://mail.example.com"
    )
    assert (
        cloudflare_temp_email.normalize_cloudflare_temp_email_base_url("https://mail.example.com/api")
        == "https://mail.example.com"
    )

    client = cloudflare_temp_email.CloudflareTempEmailClient()
    monkeypatch.setattr(client, "domain", "example.com")
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: {
            "address": "tmp-user@example.com",
            "address_id": 321,
            "jwt": "token",
        },
    )

    account_id, email = client.create_temp_email(prefix="tmp-user")

    assert account_id == 321
    assert email == "tmp-user@example.com"


def test_extract_helpers_prefer_ai_extract_metadata():
    client = cf_temp_email.CfTempEmailClient()

    assert (
        client.extract_verification_code(
            {
                "metadata": '{"ai_extract":{"type":"auth_code","result":"123456"}}',
                "subject": "ignored 654321",
            }
        )
        == "123456"
    )
    assert (
        client.extract_invite_link(
            {
                "metadata": '{"ai_extract":{"type":"auth_link","result":"https://chatgpt.com/auth/login?invite=abc"}}',
                "content": "",
            }
        )
        == "https://chatgpt.com/auth/login?invite=abc"
    )


def test_search_emails_by_recipient_filters_unrelated_address(monkeypatch):
    client = cf_temp_email.CfTempEmailClient()
    target = "tmp-user@example.com"

    monkeypatch.setattr(accounts, "load_accounts", lambda: [])

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(path, params=None):
        return _Resp(
            {
                "results": [
                    {
                        "id": 410,
                        "address": "someone-else@example.com",
                        "raw": "Subject: Other\r\n\r\nbody",
                    }
                ]
            }
        )

    monkeypatch.setattr(client, "_admin_get", fake_get)

    emails = client.search_emails_by_recipient(target, size=5)

    assert emails == []


def test_resolve_address_id_accepts_int_and_email(monkeypatch):
    client = cf_temp_email.CfTempEmailClient()

    assert client._resolve_address_id(43) == 43
    assert client._resolve_address_id("43") == 43

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(path, params=None):
        assert path == "/admin/address"
        return _Resp({"results": [{"id": 99, "name": "user@example.com"}]})

    monkeypatch.setattr(client, "_admin_get", fake_get)

    assert client._resolve_address_id("user@example.com") == 99


def test_extract_verification_code_prefers_visible_text_over_html_color_values():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": None,
        "content": """
        <html>
          <head>
            <title>Your ChatGPT code is 676952</title>
            <style>
              .top { color: #202123; }
              .body { color: #353740; }
            </style>
          </head>
          <body>
            <p>Your ChatGPT code is 676952</p>
          </body>
        </html>
        """,
    }

    assert client.extract_verification_code(email_data) == "676952"


def test_extract_verification_code_uses_plain_text_when_available():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": "Your temporary OpenAI login code is 123456",
        "content": "<html><style>.top{color:#202123}</style><body>ignored</body></html>",
    }

    assert client.extract_verification_code(email_data) == "123456"


def test_extract_verification_code_prefers_context_when_multiple_numbers():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": (
            "Request 381245 was created at 202606. "
            "Your ChatGPT code is 654321. "
            "This code expires in 10 minutes."
        ),
        "subject": "OpenAI login 111111",
    }

    assert client.extract_verification_code(email_data) == "654321"


def test_extract_verification_code_supports_trailing_context():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": "837462 is your OpenAI verification code. Do not share it.",
        "content": "<a href=\"https://example.com/t/123456\">ignore</a>",
    }

    assert client.extract_verification_code(email_data) == "837462"


def test_extract_verification_code_rejects_ambiguous_uncontextual_numbers():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": "Ticket 123456. Link token 654321.",
        "subject": "Notification",
    }

    assert client.extract_verification_code(email_data) is None


def test_extract_verification_code_allows_single_standalone_visible_code():
    client = cf_temp_email.CfTempEmailClient()

    email_data = {
        "text": "Use 246810 to continue.",
        "subject": "OpenAI",
    }

    assert client.extract_verification_code(email_data) == "246810"


def test_factory_returns_cf_temp_email_by_default(monkeypatch):
    """工厂 + 别名兼容性:`from autoteam.cloudmail import CloudMailClient` 调用零改动。"""
    monkeypatch.delenv("MAIL_PROVIDER", raising=False)
    from autoteam.cloudmail import CloudMailClient

    client = CloudMailClient()
    assert isinstance(client, cf_temp_email.CfTempEmailClient)


def test_factory_dispatches_to_maillab_when_configured(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "maillab")
    monkeypatch.setenv("MAILLAB_API_URL", "http://example.com")
    from autoteam.cloudmail import CloudMailClient
    from autoteam.mail.maillab import MaillabClient

    client = CloudMailClient()
    assert isinstance(client, MaillabClient)


def test_factory_rejects_unknown_provider(monkeypatch):
    import pytest

    monkeypatch.setenv("MAIL_PROVIDER", "made-up")
    from autoteam.mail import get_mail_client

    with pytest.raises(ValueError, match="未知 MAIL_PROVIDER"):
        get_mail_client()
