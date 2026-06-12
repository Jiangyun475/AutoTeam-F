"""save_auth_file 的 protect_team 守护行为。

根因复盘:Team 子号重登 OAuth 偶尔漂移回个人 free 工作空间,旧实现无条件
删除该邮箱所有旧文件(含 `-team-`)再写 free,导致团凭证被销毁 → 之后拿 free
token 探团 workspace 必 401 →"认证异常"死循环。守护要保证:

* 已有 team 凭证 + 传入非 team bundle(默认 protect_team=True)→ 保留 team,拒绝覆盖
* 传入 team bundle → 始终写入(刷新团凭证)
* protect_team=False(personal 转化等合法降级)→ 放行覆盖
* 无 team 凭证可保护时 → 按原行为写入
"""
import glob
import json
from pathlib import Path

import pytest

from autoteam import codex_auth


def _bundle(email, plan_type, account_id):
    return {
        "email": email,
        "plan_type": plan_type,
        "account_id": account_id,
        "access_token": f"at-{plan_type}-{account_id}",
        "refresh_token": f"rt-{plan_type}-{account_id}",
        "id_token": "id",
        "expired": 0,
    }


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    d = tmp_path / "auths"
    d.mkdir()
    monkeypatch.setattr(codex_auth, "AUTH_DIR", d)
    return d


def _files(auth_dir, email):
    return [Path(p).name for p in sorted(glob.glob(str(auth_dir / f"codex-{email}-*.json")))]


def _access_token(path):
    return json.loads(Path(path).read_text())["access_token"]


EMAIL = "yun3@yunfei.life"


def test_free_bundle_does_not_overwrite_existing_team(auth_dir):
    team_path = codex_auth.save_auth_file(_bundle(EMAIL, "team", "acctTEAM"))
    assert "-team-" in Path(team_path).name

    # Team 子号重登漂移到 free —— 默认守护必须拒绝覆盖
    returned = codex_auth.save_auth_file(_bundle(EMAIL, "free", "acctFREE"))

    assert returned == team_path  # 返回的仍是原 team 文件路径
    files = _files(auth_dir, EMAIL)
    assert files == [Path(team_path).name]  # 只剩 team,没有 free 文件被写入
    assert _access_token(team_path) == "at-team-acctTEAM"  # team token 未被污染


def test_team_bundle_replaces_old_free(auth_dir):
    free_path = codex_auth.save_auth_file(_bundle(EMAIL, "free", "acctFREE"))
    assert "-free-" in Path(free_path).name

    team_path = codex_auth.save_auth_file(_bundle(EMAIL, "team", "acctTEAM"))

    assert "-team-" in Path(team_path).name
    files = _files(auth_dir, EMAIL)
    assert files == [Path(team_path).name]  # 旧 free 被清掉,只剩 team
    assert not Path(free_path).exists()


def test_protect_team_false_allows_free_overwrite(auth_dir):
    team_path = codex_auth.save_auth_file(_bundle(EMAIL, "team", "acctTEAM"))

    # personal 转化 / fill-personal:显式放行
    free_path = codex_auth.save_auth_file(
        _bundle(EMAIL, "free", "acctFREE"), protect_team=False
    )

    assert "-free-" in Path(free_path).name
    assert not Path(team_path).exists()  # team 被合法替换
    files = _files(auth_dir, EMAIL)
    assert files == [Path(free_path).name]


def test_free_bundle_writes_when_no_team_exists(auth_dir):
    # 没有可保护的 team 文件 → 守护不生效,按原行为写 free
    free_path = codex_auth.save_auth_file(_bundle(EMAIL, "free", "acctFREE"))

    assert "-free-" in Path(free_path).name
    assert Path(free_path).exists()
    assert _access_token(free_path) == "at-free-acctFREE"


def test_team_bundle_refreshes_existing_team(auth_dir):
    codex_auth.save_auth_file(_bundle(EMAIL, "team", "acctOLD"))
    new_team = codex_auth.save_auth_file(_bundle(EMAIL, "team", "acctNEW"))

    files = _files(auth_dir, EMAIL)
    assert files == [Path(new_team).name]  # 仅保留最新 team
    assert _access_token(new_team) == "at-team-acctNEW"
