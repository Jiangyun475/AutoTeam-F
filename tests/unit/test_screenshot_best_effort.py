from autoteam import codex_auth, invite


class FailingPage:
    def screenshot(self, **_kwargs):
        raise TimeoutError("Page.screenshot: Timeout 30000ms exceeded.")


def test_codex_auth_screenshot_is_best_effort(caplog):
    codex_auth._screenshot(FailingPage(), "timeout.png")

    assert "失败，忽略" in caplog.text


def test_invite_screenshot_is_best_effort(caplog):
    invite.screenshot(FailingPage(), "timeout.png")

    assert "失败，忽略" in caplog.text
