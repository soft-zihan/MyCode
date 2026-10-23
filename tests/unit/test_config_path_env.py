"""MYCODE_CONFIG_PATH 重定向：容器里配置从 Secret 挂载点读，不读 ~/.my-code/config.json。

与 session_dir() 的 MYCODE_SESSION_DIR 同一模式（agents/core/session.py:518-524）：
空串与纯空白都要回退到默认路径，否则 K8s 里一个空 env 就会把配置指到进程 CWD。

注意：默认路径不能在模块级算。tests/conftest.py 的 autouse `isolated_home`
每个用例都把 HOME 指到 tmp_path/home，而模块级常量在 import 时求值、拿到的是
真 HOME，两边必然对不上。所以一律通过 fixture 取。
"""
import json

from agents.config import config_path, load_config, save_config


def test_defaults_to_home_when_unset(monkeypatch, isolated_home):
    monkeypatch.delenv("MYCODE_CONFIG_PATH", raising=False)
    assert config_path() == isolated_home / ".my-code" / "config.json"


def test_blank_and_whitespace_fall_back_to_home(monkeypatch, isolated_home):
    expected = isolated_home / ".my-code" / "config.json"
    for blank in ("", "   ", "\t"):
        monkeypatch.setenv("MYCODE_CONFIG_PATH", blank)
        assert config_path() == expected, f"空白值 {blank!r} 不该被当成有效路径"


def test_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "config.json"
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(target))
    assert config_path() == target


def test_load_and_save_round_trip_through_override(monkeypatch, tmp_path):
    """重定向必须对读写两端同时生效，否则云端会出现「读 Secret、写镜像层」的分裂。"""
    target = tmp_path / "config.json"
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(target))
    target.write_text(json.dumps({
        "endpoints": {"e1": {"model": "m", "base_url": "http://x", "api_key": "k"}},
        "routing": {"primary": "e1"},
    }), encoding="utf-8")

    cfg = load_config()
    assert cfg.endpoints["e1"].model == "m"
    assert cfg.routing["primary"] == "e1"

    cfg.routing["side"] = "e1"
    save_config(cfg)

    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["routing"]["side"] == "e1"
    assert on_disk["endpoints"]["e1"]["api_key"] == "k"


def test_missing_file_yields_empty_config(monkeypatch, tmp_path):
    """容器首次启动时 Secret 可能还没挂上，load_config 必须返回空配置而不是抛。"""
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "absent.json"))
    cfg = load_config()
    assert cfg.endpoints == {}
    assert cfg.routing == {}
