"""Config precedence: flag > environment > file > default, and provenance for each."""
import os

import pytest

from serenedash.config import DEFAULTS, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No global config, no inherited environment - otherwise the developer's own setup decides."""
    for var in ("SERENEDB_CONTAINER", "SERENEDB_PORT", "PGPASSWORD", "SERENEDB_DATA",
                "SERENEDASH_PERF_DIR", "SERENEDASH_INTERVAL", "SERENEDASH_CONFIG",
                "SERENEDASH_SYMBOL_PATHS", "SERENEDB_TARGET", "SERENEDB_HOST",
                "SERENEDASH_MOUSE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_name_no_particular_deployment():
    # The whole point of the config layer: a fresh checkout must not claim to know someone's
    # container name or password.
    vals, prov = load_config()
    assert vals["container"] == DEFAULTS["container"] == "serenedb"
    assert vals["password"] == ""
    assert prov["container"] == "default"


def test_file_beats_default(tmp_path, monkeypatch):
    cfg = write(tmp_path / "xdg" / "serenedash" / "config.toml", 'container = "from-file"\n')
    vals, prov = load_config()
    assert vals["container"] == "from-file"
    assert prov["container"] == str(cfg)


def test_env_beats_file(tmp_path, monkeypatch):
    write(tmp_path / "xdg" / "serenedash" / "config.toml", 'container = "from-file"\n')
    monkeypatch.setenv("SERENEDB_CONTAINER", "from-env")
    vals, prov = load_config()
    # This ordering is what makes direnv useful: a .envrc points the tool at the server the
    # directory belongs to without touching anything global.
    assert vals["container"] == "from-env"
    assert prov["container"] == "$SERENEDB_CONTAINER"


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("SERENEDB_CONTAINER", "from-env")
    vals, prov = load_config({"container": "from-flag"})
    assert vals["container"] == "from-flag"
    assert prov["container"] == "flag"


def test_unset_flag_does_not_win(monkeypatch):
    # Flags default to None so "not given" differs from "given the default value". Without that a
    # flag can never lose and the file is unreachable for any value matching a default.
    monkeypatch.setenv("SERENEDB_CONTAINER", "from-env")
    vals, _ = load_config({"container": None, "port": None})
    assert vals["container"] == "from-env"


def test_project_file_beats_global(tmp_path):
    write(tmp_path / "xdg" / "serenedash" / "config.toml", 'container = "global"\n')
    write(tmp_path / "serenedash.toml", 'container = "project"\n')
    vals, _ = load_config()
    assert vals["container"] == "project"


def test_password_command_runs_only_when_nothing_more_direct(monkeypatch):
    write_cfg = load_config({"password_command": "echo from-command"})
    assert write_cfg[0]["password"] == "from-command"
    assert write_cfg[1]["password"] == "password_command"
    monkeypatch.setenv("PGPASSWORD", "direct")
    vals, prov = load_config({"password_command": "echo from-command"})
    assert vals["password"] == "direct"


def test_symbol_paths_split_like_PATH(monkeypatch):
    monkeypatch.setenv("SERENEDASH_SYMBOL_PATHS", f"/a{os.pathsep}/b")
    vals, _ = load_config()
    assert vals["symbol_paths"] == ["/a", "/b"]


def test_interval_is_numeric_from_every_layer(monkeypatch):
    monkeypatch.setenv("SERENEDASH_INTERVAL", "2")
    vals, _ = load_config()
    assert vals["interval"] == 2.0


def test_unreadable_or_broken_file_is_skipped(tmp_path):
    write(tmp_path / "xdg" / "serenedash" / "config.toml", "this is not = valid toml [[[\n")
    vals, prov = load_config()
    assert vals["container"] == "serenedb"
    assert prov["container"] == "default"


def test_mouse_is_a_yes_no_and_0_means_no():
    # The environment and the command line both deliver this as text, and bool("0") is True — a
    # variable set to 0 to turn tooltips off would have turned them on.
    assert load_config()[0]["mouse"] is True
    for off in ("0", "false", "no", "off", ""):
        os.environ["SERENEDASH_MOUSE"] = off
        try:
            # An empty value is "not set" to the loader, so it keeps the default rather than
            # reading as false; every other spelling of no means no.
            assert load_config()[0]["mouse"] is (off == ""), off
        finally:
            del os.environ["SERENEDASH_MOUSE"]


def test_a_config_file_can_turn_tooltips_off_for_good(tmp_path):
    write(tmp_path / "serenedash.toml", "mouse = false\n")
    vals, prov = load_config()
    assert vals["mouse"] is False
    assert prov["mouse"].endswith("serenedash.toml")
    # And the flag still beats it, in both directions.
    assert load_config({"mouse": True})[0]["mouse"] is True
