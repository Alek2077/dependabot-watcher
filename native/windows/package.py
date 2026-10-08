"""Package an explicitly selected, already verified Windows publish directory.

This does not build, install, register, launch or publish the component. SDKs
and NuGet caches stay outside the archive; package notices are copied from the
build's resolved dependency graph. SHA-256 provides integrity, not authenticity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def real_path(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.resolve() != absolute or any(parent.is_symlink() for parent in (absolute, *absolute.parents)):
        raise ValueError("Packaging paths must not use filesystem aliases or links.")
    return absolute


def bounded_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("Build metadata exceeds its bound.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Invalid build metadata.")
    return value


def archive_key(name: str) -> str:
    parts = name.split("/")
    reserved_devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if len(name) > 1024 or any(not part or part in {".", ".."} or part.endswith((".", " "))
           or part.split(".")[0].casefold() in reserved_devices
           or any(character in part for character in '\\:<>"|?*')
           or any(ord(character) < 32 for character in part) for part in parts):
        raise ValueError("Archive path is not safe for Windows extraction.")
    return name.casefold()


def selected_files(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Select a real published component directory.")
    files: list[Path] = []
    seen: set[str] = set()
    count = 0
    for path in directory.rglob("*"):
        count += 1
        if count > 20_000:
            raise ValueError("Published file count exceeds its bound.")
        if path.is_symlink():
            raise ValueError("Published component contains a symbolic link.")
        real_path(path)
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        key = archive_key(relative.as_posix())
        if key in seen:
            raise ValueError("Published files collide on Windows.")
        seen.add(key)
        forbidden = {"secrets", ".git", "state", "obj", "bin", "packages", ".nuget", "sdk", "dotnet-home", "node_modules", "src"}
        if path.suffix.lower() in {".jsonl", ".log", ".toml", ".db", ".sqlite"} or any(
            part.lower().startswith(".env") or part.lower() in forbidden
            for part in relative.parts
        ):
            raise ValueError("Published component contains configuration or private evidence.")
        if path.suffix.lower() not in {".dll", ".exe", ".json", ".mui", ".html", ".png", ".pri", ".winmd", ".pdb"}:
            raise ValueError("Published component contains an unexpected file class.")
        if path.suffix.lower() == ".json" and path.name not in {
            "WatcherNativeProof.deps.json", "WatcherNativeProof.runtimeconfig.json",
            "workloads.json", "workloads.365.json", "workloads.j32.json", "workloads.lnl.json",
            "workloads.qnn.json", "workloads.stx.json"
        }:
            raise ValueError("Published component contains unexpected JSON metadata.")
        if path.suffix.lower() != ".pdb":
            files.append(path)
    for name in ("WatcherNativeProof.exe", "WatcherNativeProof.dll", "WatcherNativeProof.deps.json",
                 "WatcherNativeProof.runtimeconfig.json", "Microsoft.WindowsAppRuntime.Insights.Resource.dll"):
        if directory / name not in files:
            raise ValueError(f"Published component is missing {name}.")
    return sorted(files)


def dependency_notices(assets_file: Path, package_cache: Path | None = None) -> dict[str, bytes]:
    assets = bounded_json(real_path(assets_file))
    if len(assets["libraries"]) > 1000:
        raise ValueError("Dependency count exceeds its bound.")
    folders = [package_cache] if package_cache is not None else [Path(name) for name in assets["packageFolders"]]
    notices: dict[str, bytes] = {}
    notice_bytes = 0
    scanned = 0
    for identity, library in sorted(assets["libraries"].items()):
        if library.get("type") != "package":
            continue
        package_path = Path(library["path"])
        if package_path.is_absolute() or ".." in package_path.parts or not re.fullmatch(r"[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+", identity):
            raise ValueError("Invalid resolved package identity.")
        archive_key(identity)
        if package_path.as_posix().casefold() != identity.casefold():
            raise ValueError("Resolved package path does not match its identity.")
        candidates = [folder / package_path for folder in folders if (folder / package_path).is_dir()]
        if len(candidates) != 1:
            raise ValueError(f"Resolved dependency is missing or ambiguous: {identity}.")
        package = real_path(candidates[0])
        nuspecs = list(package.glob("*.nuspec"))
        if len(nuspecs) != 1 or nuspecs[0].stat().st_size > 512 * 1024:
            raise ValueError("Resolved dependency metadata is missing or oversized.")
        metadata = ET.fromstring(real_path(nuspecs[0]).read_bytes()).find("{*}metadata")
        if metadata is None or f"{metadata.findtext('{*}id')}/{metadata.findtext('{*}version')}".casefold() != identity.casefold():
            raise ValueError("Dependency notice identity does not match resolved metadata.")
        declared_files = library.get("files")
        if not isinstance(declared_files, list) or any(not isinstance(name, str) for name in declared_files):
            raise ValueError("Resolved dependency file list is missing or invalid.")
        scanned += len(declared_files)
        if scanned > 50_000:
            raise ValueError("Dependency notice scan exceeds its bound.")
        # The resolved graph already lists package members, including nested
        # notices. Do not walk unrelated files in a potentially huge SDK cache.
        for relative in declared_files:
            if any(word in Path(relative).name.lower() for word in ("license", "notice")):
                archive_key(relative)
                file = real_path(package / relative)
                if not file.is_file():
                    raise ValueError("Resolved dependency notice is unavailable.")
                if file.stat().st_size > 8 * 1024**2:
                    raise ValueError("Dependency notice exceeds the package bound.")
                name = f"licenses/{identity}/{relative}"
                archive_key(name)
                notice_bytes += file.stat().st_size
                if notice_bytes > 32 * 1024**2 or len(notices) >= 2000:
                    raise ValueError("Dependency notices exceed the aggregate bound.")
                notices[name] = file.read_bytes()
    if not notices:
        raise ValueError("Resolved dependencies supplied no license notices.")
    return notices


def package_component(published: Path, assets: Path, output: Path, version: str,
                      commit: str, *, preview: bool, package_cache: Path | None = None) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Use an explicit package version.")
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise ValueError("Use the full verified source commit.")
    published = real_path(published)
    output = real_path(output)
    if output == published or published in output.parents:
        raise ValueError("Package output must be outside the publish directory.")
    files = selected_files(published)
    graph = bounded_json(real_path(assets))
    published_graph = bounded_json(published / "WatcherNativeProof.deps.json")
    for identity, library in published_graph["libraries"].items():
        if library.get("type") == "package":
            resolved = graph["libraries"].get(identity)
            if (not isinstance(resolved, dict) or not isinstance(resolved.get("sha512"), str)
                    or f"sha512-{resolved['sha512']}" != library.get("sha512")):
                raise ValueError("Resolved dependency graph does not match the published component.")
    notices = dependency_notices(assets, package_cache)
    repository = Path(__file__).resolve().parents[2]
    additions = {"LICENSE": (repository / "LICENSE").read_bytes(),
                 "README.md": (repository / "native/windows/README.md").read_bytes(), **notices}
    generated = ["component-manifest.json", *additions]
    reserved = {archive_key(name) for name in generated}
    if len(reserved) != len(generated) or any(archive_key(path.relative_to(published).as_posix()) in reserved for path in files):
        raise ValueError("Publish files collide with package metadata.")
    result: dict[str, object] = {"status": "preview" if preview else "packaged", "version": version,
        "source_commit": commit, "platform": "windows-x64", "runtime_files": len(files),
        "uncompressed_runtime_bytes": sum(path.stat().st_size for path in files),
        "license_files": len(notices), "signing": "unsigned", "published": False}
    if preview:
        return result
    output.mkdir(parents=True, exist_ok=True)
    name = f"dependabot-watcher-review-{version}-windows-x64.zip"
    destination = output / name
    if destination.exists():
        raise ValueError("Refusing to replace an existing package.")
    # Stream into one temporary ZIP, never duplicate the self-contained runtime.
    with tempfile.TemporaryDirectory(prefix=".watcher-package-", dir=output) as temporary:
        candidate = Path(temporary) / name
        records: list[dict[str, object]] = []
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                real_path(path)
                before = path.stat()
                relative = path.relative_to(published).as_posix()
                digest = hashlib.sha256()
                with path.open("rb") as source, archive.open(relative, "w", force_zip64=True) as target:
                    while chunk := source.read(1024**2):
                        digest.update(chunk)
                        target.write(chunk)
                after = path.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("Published files changed during packaging.")
                records.append({"path": relative, "bytes": before.st_size, "sha256": digest.hexdigest()})
            for relative, content in additions.items():
                archive.writestr(relative, content)
                records.append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            archive.writestr("component-manifest.json", json.dumps({**result, "files": records}, indent=2) + "\n")
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None:
                raise ValueError("Package integrity verification failed.")
        # Same-filesystem exclusive link publishes a complete ZIP atomically,
        # without replacing another package or leaving a partial destination.
        os.link(candidate, destination)
    with destination.open("rb") as archive_file:
        archive_hash = hashlib.file_digest(archive_file, "sha256").hexdigest()
    result.update({"archive": str(destination), "archive_bytes": destination.stat().st_size,
                   "sha256": archive_hash})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-directory", type=Path, required=True)
    parser.add_argument("--assets-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--package-cache", type=Path, help="Resolved NuGet cache, when packaging a Windows build from WSL.")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    try:
        result = package_component(args.published_directory, args.assets_file, args.output_directory,
                                   args.version, args.source_commit, preview=args.preview, package_cache=args.package_cache)
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError, zipfile.BadZipFile) as error:
        parser.exit(2, f"Component packaging failed: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
