"""The 0.5.0 backport must not leave the Docker grant writable when absent."""

import json
import os

import pytest

from kiro_crew import sandbox


def test_absent_grant_is_disabled_private_and_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    target = tmp_path / "docker_registry_access.json"
    assert sandbox._seal_docker_grant_target() == str(target)
    assert json.loads(target.read_text()) == {}
    assert target.stat().st_mode & 0o077 == 0
    target.write_text('{"enabled": true, "permanent": true}')
    sandbox._seal_docker_grant_target()
    assert json.loads(target.read_text())["enabled"] is True


@pytest.mark.parametrize("alias", ["symlink", "dangling", "hardlink", "directory"])
def test_alias_or_invalid_grant_refuses_spawn(tmp_path, monkeypatch, alias):
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    target = tmp_path / "docker_registry_access.json"
    other = tmp_path / "other"
    if alias != "dangling":
        other.write_text("{}")
    if alias in ("symlink", "dangling"):
        target.symlink_to(other)
    elif alias == "hardlink":
        os.link(other, target)
    else:
        target.mkdir()
    with pytest.raises(RuntimeError, match="regular, unaliased"):
        sandbox._seal_docker_grant_target()


def test_launcher_contains_readonly_grant_even_when_exposure_off(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    script = sandbox._build_launcher_script("standard", expose_docker_config=False)
    assert str(tmp_path / "docker_registry_access.json") in script
    assert "os.path.isdir(target) or os.path.isfile(target)" in script
