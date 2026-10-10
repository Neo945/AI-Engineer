"""Static guards for the deployment manifests and CI workflows.

These tests do not touch a cluster: they load the committed YAML and assert the
contract that matters operationally (probes pointed at the right endpoints, a
migration initContainer present, kustomization resources exist, workflow jobs
run the same checks as ``make lint`` / ``make typecheck`` / ``make test-unit``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_all(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _docs_by_kind(path: Path) -> dict[str, dict[str, Any]]:
    return {doc["kind"]: doc for doc in _load_all(path)}


def test_every_k8s_manifest_parses() -> None:
    files = sorted(K8S_DIR.glob("*.yaml"))
    assert files, "expected k8s manifests"
    for manifest in files:
        assert _load_all(manifest), f"{manifest.name} parsed to no documents"


def test_kustomization_resources_exist() -> None:
    kustomization = _load(K8S_DIR / "kustomization.yaml")
    resources = kustomization["resources"]
    names = {Path(resource).name for resource in resources}
    on_disk = {path.name for path in K8S_DIR.glob("*.yaml")}
    assert names <= on_disk, f"kustomization references missing files: {names - on_disk}"
    # The example secret is intentionally not applied.
    assert "secret.example.yaml" not in names


def test_api_deployment_has_migration_initcontainer_and_probes() -> None:
    deployment = _docs_by_kind(K8S_DIR / "api.yaml")["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    init_names = [container["name"] for container in pod["initContainers"]]
    assert "migrate" in init_names

    api = next(c for c in pod["containers"] if c["name"] == "api")
    assert api["readinessProbe"]["httpGet"]["path"] == "/api/v1/readyz"
    assert api["livenessProbe"]["httpGet"]["path"] == "/api/v1/healthz"

    env_sources = {next(iter(entry)) for entry in api["envFrom"]}
    assert env_sources == {"configMapRef", "secretRef"}


def test_dependencies_use_expected_images() -> None:
    postgres = _docs_by_kind(K8S_DIR / "postgres.yaml")["StatefulSet"]
    assert postgres["spec"]["template"]["spec"]["containers"][0]["image"].startswith(
        "pgvector/pgvector"
    )
    redis = _docs_by_kind(K8S_DIR / "redis.yaml")["Deployment"]
    assert redis["spec"]["template"]["spec"]["containers"][0]["image"].startswith("redis:")


def test_configmap_has_no_secret_material() -> None:
    data = _load(K8S_DIR / "configmap.yaml")["data"]
    for forbidden in ("DATABASE_URL", "AUTH_SECRET", "LLM_API_KEY", "POSTGRES_PASSWORD"):
        assert forbidden not in data, f"{forbidden} must not live in the ConfigMap"


def test_ci_workflow_runs_the_quality_gates() -> None:
    workflow = _load(WORKFLOWS_DIR / "ci.yml")
    steps = workflow["jobs"]["lint-typecheck-test"]["steps"]
    run_commands = " ".join(step.get("run", "") for step in steps)
    assert "ruff check app tests" in run_commands
    assert "ruff format --check app tests" in run_commands
    assert "mypy app" in run_commands
    assert 'pytest -m "not integration"' in run_commands


def test_release_workflow_builds_the_image() -> None:
    workflow = _load(WORKFLOWS_DIR / "release.yml")
    steps = workflow["jobs"]["build-and-push"]["steps"]
    uses = {step.get("uses", "") for step in steps}
    assert "docker/build-push-action@v6" in uses
    build = next(step for step in steps if step.get("uses") == "docker/build-push-action@v6")
    assert build["with"]["file"] == "infra/Dockerfile"
    assert build["with"]["push"] is True
