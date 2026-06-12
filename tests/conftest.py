"""Global test fixtures.

把模块级 ``default_machine`` 的 JSONL 日志重定向到每个测试的 tmp 路径。

没有这层隔离时,任何经过 ``update_account(status=...)`` 的测试都会把 fixture
邮箱(a@x.com / user@example.com / ...)的状态流转追加进项目根目录的**真实**
state_log.jsonl;而 api._auto_check_burn_guard_status / 认证修复熔断都从这个
文件统计最近的 active→exhausted / auth_invalid 事件,测试污染可能直接误触发
生产熔断、暂停自动补位。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_state_log(tmp_path, monkeypatch):
    from autoteam import account_state

    monkeypatch.setattr(
        account_state.default_machine,
        "_log_path",
        tmp_path / "state_log.jsonl",
        raising=False,
    )
    yield


@pytest.fixture(autouse=True)
def _reset_live_quota_cache():
    """清空 get_status 的实时额度缓存,避免跨测试串味(模块级缓存会被多个
    用例共享,前一个用例的探测结果会污染后一个不同 mock 的断言)。"""
    try:
        from autoteam import api
    except Exception:
        yield
        return
    api._reset_live_quota_cache()
    yield
    api._reset_live_quota_cache()
