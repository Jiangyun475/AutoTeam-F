from autoteam import api
from autoteam.mail.cf_temp_email import CfTempEmailClient


def test_extract_mail_codes_prefers_contextual_code_over_other_digits():
    result = api._extract_mail_codes(
        "Your OpenAI verification code",
        "Your verification code is 123456.\nReference: 654321\nFooter id: 111222",
    )

    assert result == ["123456"]


def test_extract_mail_codes_does_not_return_multiple_ambiguous_standalone_digits():
    result = api._extract_mail_codes("Invoice 123456\nTicket 654321\nFooter 111222")

    assert result == []


def test_extract_mail_codes_dedupes_repeated_single_standalone_code():
    result = api._extract_mail_codes("123456", "<div>123456</div>", "plain copy 123456")

    assert result == ["123456"]


def test_extract_mail_codes_supports_code_before_keyword():
    result = api._extract_mail_codes("987654 is your OpenAI login code")

    assert result == ["987654"]


def test_cf_temp_email_normalize_mail_record_preserves_metadata(monkeypatch):
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "secret")
    client = CfTempEmailClient()

    mail = client._normalize_mail_record(
        {
            "id": 7,
            "address": "user@example.com",
            "raw": "Subject: Test\r\nFrom: sender@example.com\r\nTo: user@example.com\r\n\r\nbody",
            "metadata": '{"ai_extract":{"type":"auth_code","result":"123456"}}',
        }
    )

    assert mail["metadata"] == '{"ai_extract":{"type":"auth_code","result":"123456"}}'


def test_get_mail_inbox_returns_visible_body(monkeypatch):
    class FakeClient:
        def login(self):
            return "ok"

        def search_emails_by_recipient(self, address, size=10):
            assert address == "user@example.com"
            return [
                {
                    "emailId": 1,
                    "subject": "Code",
                    "sendEmail": "noreply@example.com",
                    "content": "<html><body><p>Your verification code is 123456.</p><p>Full body line.</p></body></html>",
                }
            ]

        def extract_verification_code(self, mail):
            return "123456"

    monkeypatch.setattr("autoteam.mail.get_mail_client", lambda: FakeClient())

    result = api.get_mail_inbox("USER@example.com")

    item = result["items"][0]
    assert item["codes"] == ["123456"]
    assert "Your verification code is 123456." in item["body"]
    assert "Full body line." in item["body"]
    assert item["preview"] == item["body"][:600]
