from __future__ import annotations

from dependabot_watcher.manifests import (
    collect_manifest_evidence,
    parse_python_requirements_includes,
)
from dependabot_watcher.github import GitHubError


class ManifestClient:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.requests: list[tuple[str, str]] = []

    def fetch_text_file(
        self,
        repository: str,
        path: str,
        ref: str,
        max_bytes: int,
    ) -> str:
        self.requests.append((path, ref))
        return self.files[path]


def test_requirements_parser_resolves_local_includes_and_ignores_unsafe_targets() -> None:
    relationships = parse_python_requirements_includes(
        "config/requirements-dev.txt",
        """
        -r ../requirements.txt
        --constraint=constraints.txt
        -r https://example.invalid/remote.txt
        -r ../../../outside.txt
        package==1.0
        """,
    )

    assert relationships == (
        {"path": "requirements.txt", "relation": "requirement"},
        {"path": "config/constraints.txt", "relation": "constraint"},
    )


def test_manifest_collection_follows_bounded_inclusion_graph() -> None:
    client = ManifestClient(
        {
            "requirements-dev.txt": "-r requirements.txt\n-c constraints.txt\n",
            "requirements.txt": "-r shared/base.txt\n",
            "constraints.txt": "example-lib==2.0\n",
            "shared/base.txt": "base-lib==1.0\n",
        }
    )

    evidence = collect_manifest_evidence(
        repository="example-org/example-app",
        base_sha="base123",
        seed_paths=["requirements-dev.txt"],
        client=client,  # type: ignore[arg-type]
        max_files=10,
        max_file_bytes=1000,
        max_depth=3,
    )

    assert evidence["status"] == "complete"
    assert {item["path"] for item in evidence["files"]} == {
        "requirements-dev.txt",
        "requirements.txt",
        "constraints.txt",
        "shared/base.txt",
    }
    assert {
        (item["source_manifest"], item["included_manifest"])
        for item in evidence["relationships"]
    } == {
        ("requirements-dev.txt", "requirements.txt"),
        ("requirements-dev.txt", "constraints.txt"),
        ("requirements.txt", "shared/base.txt"),
    }


def test_manifest_collection_reports_depth_truncation() -> None:
    client = ManifestClient(
        {
            "requirements-dev.txt": "-r requirements.txt\n",
        }
    )

    evidence = collect_manifest_evidence(
        repository="example-org/example-app",
        base_sha="base123",
        seed_paths=["requirements-dev.txt"],
        client=client,  # type: ignore[arg-type]
        max_files=10,
        max_file_bytes=1000,
        max_depth=0,
    )

    assert evidence["status"] == "partial"
    assert evidence["truncations"][0]["surface"] == "manifest_depth"
    assert client.requests == [("requirements-dev.txt", "base123")]


def test_manifest_collection_bounds_failed_seed_attempts() -> None:
    class FailingClient(ManifestClient):
        def fetch_text_file(
            self,
            repository: str,
            path: str,
            ref: str,
            max_bytes: int,
        ) -> str:
            self.requests.append((path, ref))
            raise GitHubError("missing")

    client = FailingClient({})
    evidence = collect_manifest_evidence(
        repository="example-org/example-app",
        base_sha="base123",
        seed_paths=[f"requirements-{index}.txt" for index in range(100)],
        client=client,  # type: ignore[arg-type]
        max_files=2,
        max_file_bytes=1000,
        max_depth=3,
    )

    assert evidence["status"] == "partial"
    assert len(client.requests) == 2
    assert len(evidence["errors"]) == 2
    assert any(item["surface"] == "manifest_files" for item in evidence["truncations"])


def test_manifest_collection_bounds_includes_relationships_and_requests() -> None:
    includes = "".join(f"-r requirements-{index}.txt\n" for index in range(100))
    client = ManifestClient(
        {
            "requirements.txt": includes,
            "requirements-0.txt": "example==1\n",
        }
    )
    evidence = collect_manifest_evidence(
        repository="example-org/example-app",
        base_sha="base123",
        seed_paths=["requirements.txt"],
        client=client,  # type: ignore[arg-type]
        max_files=2,
        max_file_bytes=10000,
        max_depth=3,
    )

    assert evidence["status"] == "partial"
    assert len(client.requests) == 2
    assert len(evidence["relationships"]) == 2
    assert len(evidence["files"][0]["includes"]) == 2
    assert any(
        item["surface"] == "manifest_relationships"
        for item in evidence["truncations"]
    )


def test_manifest_collection_rejects_unsafe_seed_without_fetching() -> None:
    client = ManifestClient({})
    evidence = collect_manifest_evidence(
        repository="example-org/example-app",
        base_sha="base123",
        seed_paths=["/requirements.txt", "../requirements-dev.txt"],
        client=client,  # type: ignore[arg-type]
        max_files=10,
        max_file_bytes=1000,
        max_depth=3,
    )

    assert evidence["status"] == "partial"
    assert client.requests == []
    assert len(evidence["errors"]) == 2
