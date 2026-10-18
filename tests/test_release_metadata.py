from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def release_metadata():
    path = Path(__file__).parents[1] / "scripts/release_metadata.py"
    spec = importlib.util.spec_from_file_location("release_metadata", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.metadata


@pytest.mark.parametrize("version,prerelease", [("0.4.0a1", "true"), ("0.4.0b2", "true"),
    ("0.4.0rc1", "true"), ("0.163.12", "false"), ("1.0.0", "false")])
def test_exact_identity_and_channel(tmp_path, release_metadata, version, prerelease):
    (tmp_path / "src/dependabot_watcher").mkdir(parents=True)
    (tmp_path / "src/dependabot_watcher/__init__.py").write_text(f'__version__ = "{version}"\n')
    notes = tmp_path / "docs/release-notes" / f"{version}.md"
    notes.parent.mkdir(parents=True)
    with pytest.raises(ValueError, match="notes"):
        release_metadata(tmp_path)
    notes.write_text("Release changes")
    assert release_metadata(tmp_path, "v" + version)["prerelease"] == prerelease
    with pytest.raises(ValueError, match="tag"):
        release_metadata(tmp_path, "v0.1.0")


@pytest.mark.parametrize("version", ["0.04.0", "0.4", "0.4.0-alpha.1", "0.4.0a0", "0.4.0+local"])
def test_unsupported_version_refused(tmp_path, release_metadata, version):
    (tmp_path / "src/dependabot_watcher").mkdir(parents=True)
    (tmp_path / "src/dependabot_watcher/__init__.py").write_text(f'__version__ = "{version}"\n')
    with pytest.raises(ValueError, match="major.minor.patch"):
        release_metadata(tmp_path)


def test_publication_dependency_and_channel_contract():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["build"]["needs"] == "metadata"
    assert jobs["build-windows"]["needs"] == "metadata"
    pypi = jobs["publish-pypi"]
    assert "needs.metadata.outputs.prerelease == 'false'" in pypi["if"]
    assert "github.event.repository.private == false" in pypi["if"]
    assert "github.event_name == 'push'" in pypi["if"]
    github = jobs["publish-github"]
    assert set(github["needs"]) == {"metadata", "build", "build-windows", "publish-pypi"}
    assert "always()" in github["if"]
    assert "needs.build.result == 'success'" in github["if"]
    assert "needs.build-windows.result == 'success'" in github["if"]
    assert "needs.metadata.outputs.prerelease == 'true' || needs.publish-pypi.result == 'success'" in github["if"]
    publish = github["steps"][-1]["run"]
    assert "--prerelease --latest=false" in publish
    assert '--notes-file "$RELEASE_NOTES"' in publish and "--generate-notes" not in publish


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release publisher uses the hosted Linux Bash shell")
@pytest.mark.parametrize("prerelease", ["true", "false"])
def test_release_command_arguments_without_network(tmp_path, prerelease):
    """Execute the real YAML shell block; replace only the external gh write."""
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/release.yml").read_text())
    command = workflow["jobs"]["publish-github"]["steps"][-1]["run"]
    for directory in ("dist", "release-native", "tools"):
        (tmp_path / directory).mkdir()
    (tmp_path / "dist/backend.whl").touch()
    (tmp_path / "release-native/window.zip").touch()
    notes = tmp_path / "notes.md"
    notes.write_text("User-facing release notes")
    recorder = tmp_path / "tools/gh"
    recorder.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    recorder.chmod(0o700)
    result = subprocess.run(["bash", "-e", "-c", command], cwd=tmp_path,
        env={**os.environ, "PATH": str(recorder.parent) + os.pathsep + os.environ.get("PATH", ""),
             "IS_PRERELEASE": prerelease, "RELEASE_NOTES": str(notes),
             "GITHUB_REF_NAME": "v0.4.0a1" if prerelease == "true" else "v0.4.0",
             "GITHUB_REPOSITORY": "example-org/example-app"},
        text=True, capture_output=True, check=True)
    arguments = json.loads(result.stdout)
    assert arguments[:2] == ["release", "create"]
    assert "dist/backend.whl" in arguments and "release-native/window.zip" in arguments
    assert arguments[arguments.index("--notes-file") + 1] == str(notes)
    assert "--verify-tag" in arguments
    assert ("--prerelease" in arguments) == (prerelease == "true")
    assert ("--latest=false" in arguments) == (prerelease == "true")


def test_publication_checks_require_annotated_tag_and_exact_commit():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/release.yml").read_text())
    for job in ("publish-pypi", "publish-github"):
        verification = next(step["run"] for step in workflow["jobs"][job]["steps"]
                            if step.get("name") == "Verify the release tag still names the built commit")
        assert 'git cat-file -t FETCH_HEAD' in verification
        assert 'test "$(git cat-file -t FETCH_HEAD)" = tag' in verification
        assert 'FETCH_HEAD^{commit}' in verification and '"$GITHUB_SHA"' in verification


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="Requires real Git/Bash")
@pytest.mark.parametrize("annotated", [True, False])
def test_fetched_tag_object_gate_with_real_git(tmp_path, annotated):
    source = tmp_path / "source"
    consumer = tmp_path / "consumer"
    source.mkdir()
    consumer.mkdir()
    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()
    git(source, "init", "--initial-branch=main")
    git(source, "-c", "user.name=Release test", "-c", "user.email=release@example.invalid",
        "commit", "--allow-empty", "-m", "Fixture")
    head = git(source, "rev-parse", "HEAD")
    tag_arguments = ["-a", "-m", "Alpha"] if annotated else []
    git(source, "-c", "user.name=Release test", "-c", "user.email=release@example.invalid",
        "tag", *tag_arguments, "v0.4.0a1")
    git(consumer, "init")
    git(consumer, "remote", "add", "origin", source.as_uri())
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/release.yml").read_text())
    verification = next(step["run"] for step in workflow["jobs"]["publish-github"]["steps"]
                        if step.get("name") == "Verify the release tag still names the built commit")
    result = subprocess.run(["bash", "-e", "-c", verification], cwd=consumer,
        env={**os.environ, "GITHUB_REF_NAME": "v0.4.0a1", "GITHUB_SHA": head}, capture_output=True)
    assert (result.returncode == 0) == annotated, result.stderr
