from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def packager():
    path = Path(__file__).parents[1] / "native/windows/package.py"
    spec = importlib.util.spec_from_file_location("native_packager", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    published = tmp_path / "published"
    published.mkdir()
    for name in ("WatcherNativeProof.exe", "WatcherNativeProof.dll", "WatcherNativeProof.deps.json",
                 "WatcherNativeProof.runtimeconfig.json", "Microsoft.WindowsAppRuntime.Insights.Resource.dll"):
        (published / name).write_bytes(b"synthetic runtime fixture")
    (published / "WatcherNativeProof.pdb").write_bytes(b"private developer source paths")
    (published / "WatcherNativeProof.deps.json").write_text(json.dumps({"libraries": {
        "Example/1.0.0": {"type": "package", "sha512": "sha512-fixture"}}}))
    package = tmp_path / "packages/example/1.0.0"
    package.mkdir(parents=True)
    (package / "LICENSE.txt").write_text("Synthetic dependency license")
    (package / "example.nuspec").write_text("<package><metadata><id>Example</id><version>1.0.0</version></metadata></package>")
    assets = tmp_path / "project.assets.json"
    assets.write_text(json.dumps({"packageFolders": {str(tmp_path / "packages"): {}},
        "libraries": {"Example/1.0.0": {"type": "package", "path": "example/1.0.0", "sha512": "fixture", "files": ["LICENSE.txt", "example.nuspec"]}}}))
    return published, assets, tmp_path / "output"


def test_preview_creates_nothing(packager, inputs):
    published, assets, output = inputs
    result = packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)
    assert result["status"] == "preview" and result["published"] is False
    assert not output.exists()


def test_package_contains_runtime_manifest_and_notices_but_no_symbols(packager, inputs):
    published, assets, output = inputs
    result = packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=False)
    with zipfile.ZipFile(result["archive"]) as archive:
        manifest = json.loads(archive.read("component-manifest.json"))
        assert manifest["source_commit"] == "a" * 40 and manifest["signing"] == "unsigned"
        assert "licenses/Example/1.0.0/LICENSE.txt" in archive.namelist()
        assert "LICENSE" in archive.namelist() and "README.md" in archive.namelist()
        assert not any(name.endswith(".pdb") for name in archive.namelist())
        assert len(manifest["files"]) == len(archive.namelist()) - 1
    with pytest.raises(ValueError, match="replace"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=False)


@pytest.mark.parametrize("name", ["events.1.jsonl", "profile.toml", ".env", "secrets/key.json", "state/private.json"])
def test_private_state_is_refused(packager, inputs, name):
    published, assets, output = inputs
    target = published / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("private fixture")
    with pytest.raises(ValueError, match="private evidence"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)
    assert not output.exists()


def test_package_refuses_missing_resource_and_nested_output(packager, inputs):
    published, assets, output = inputs
    with pytest.raises(ValueError, match="outside"):
        packager.package_component(published, assets, published / "output", "0.3.0", "a" * 40, preview=True)
    (published / "Microsoft.WindowsAppRuntime.Insights.Resource.dll").unlink()
    with pytest.raises(ValueError, match="missing"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)


@pytest.mark.parametrize("name", ["obj/project.assets.json", "packages/archive.nupkg", "source.cs", "private.json", "LICENSE"])
def test_polluted_publish_tree_is_refused(packager, inputs, name):
    published, assets, output = inputs
    target = published / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("unexpected fixture")
    with pytest.raises(ValueError):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)


def test_notice_identity_and_graph_must_match(packager, inputs):
    published, assets, output = inputs
    data = json.loads(assets.read_text())
    data["libraries"]["Example/1.0.0"]["sha512"] = "different"
    assets.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="graph"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)
    data["libraries"]["Example/1.0.0"]["sha512"] = "fixture"
    assets.write_text(json.dumps(data))
    package = assets.parent / "packages/example/1.0.0"
    (package / "example.nuspec").write_text("<package><metadata><id>Wrong</id><version>1.0.0</version></metadata></package>")
    with pytest.raises(ValueError, match="identity"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)


def test_traversal_and_case_collisions_refused(packager, inputs):
    with pytest.raises(ValueError, match="safe"):
        packager.archive_key("licenses/../../license.txt")
    with pytest.raises(ValueError, match="safe"):
        packager.archive_key("licenses/con.txt")
    published, assets, output = inputs
    (published / "watchernativeproof.DLL").write_text("alias")
    with pytest.raises(ValueError, match="collide"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)


def test_output_alias_is_refused(packager, inputs):
    published, assets, output = inputs
    output.symlink_to(published, target_is_directory=True)
    with pytest.raises(ValueError, match="aliases"):
        packager.package_component(published, assets, output / "nested", "0.3.0", "a" * 40, preview=True)


def test_oversized_assets_refused_before_parse(packager, inputs):
    published, assets, output = inputs
    with assets.open("wb") as file:
        file.truncate(8 * 1024**2 + 1)
    with pytest.raises(ValueError, match="bound"):
        packager.package_component(published, assets, output, "0.3.0", "a" * 40, preview=True)
