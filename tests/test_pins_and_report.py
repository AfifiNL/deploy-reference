"""Static checks on the supply chain: everything the build and the cluster pull is pinned."""

from __future__ import annotations

import re

import pytest

from conftest import ROOT
from deploy_reference.e2e import Report, Step, StepFailed, wait_for


def test_dockerfile_pins_every_base_image_by_digest() -> None:
    froms = re.findall(r"^FROM (\S+)", (ROOT / "image" / "Dockerfile").read_text(), re.M)
    assert froms
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", f) for f in froms), froms


def test_no_floating_dockerfile_frontend() -> None:
    assert "# syntax=" not in (ROOT / "image" / "Dockerfile").read_text()


def test_service_commit_agrees_everywhere() -> None:
    sha = re.search(
        r"CITED_ANSWERS_SHA:-([0-9a-f]{40})", (ROOT / "scripts" / "build-image.sh").read_text()
    )
    assert sha
    full = sha.group(1)
    short = full[:7]
    lock = (ROOT / "image" / "otel-requirements.service-lock.sha256").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", lock)
    chart = (ROOT / "charts" / "cited-answers" / "Chart.yaml").read_text()
    assert f'appVersion: "{short}"' in chart
    values = (ROOT / "charts" / "cited-answers" / "values.yaml").read_text()
    assert f'tag: "{short}"' in values
    e2e = (ROOT / "src" / "deploy_reference" / "e2e.py").read_text()
    assert f'IMAGE = "cited-answers:{short}"' in e2e


def test_metrics_server_image_pinned_by_digest() -> None:
    e2e = (ROOT / "src" / "deploy_reference" / "e2e.py").read_text()
    assert re.search(r"metrics-server/metrics-server@\"\s*\"sha256:[0-9a-f]{64}\"", e2e)
    assert "METRICS_SERVER_IMAGE}" in e2e  # used by `kubectl set image`


def test_service_source_is_pinned_to_a_full_commit() -> None:
    script = (ROOT / "scripts" / "build-image.sh").read_text()
    assert re.search(r"CITED_ANSWERS_SHA:-[0-9a-f]{40}\}", script)
    assert 'rev-parse HEAD)" == "$SHA"' in script


def test_otel_requirements_are_hash_pinned() -> None:
    reqs = (ROOT / "image" / "otel-requirements.txt").read_text()
    pkgs = re.findall(r"^([a-z0-9-]+)==", reqs, re.M)
    assert "opentelemetry-instrumentation-fastapi" in pkgs
    assert reqs.count("--hash=sha256:") >= len(pkgs)


def test_tool_installer_pins_versions_and_checksums() -> None:
    script = (ROOT / "scripts" / "install-tools.sh").read_text()
    entries = re.findall(r'^  "([a-z]+)\|([^|]+)\|(https://[^|]+)\|([0-9a-f]{64})\|', script, re.M)
    assert {e[0] for e in entries} == {
        "kind",
        "kubectl",
        "helm",
        "terraform",
        "tflint",
        "kubeconform",
    }
    assert "sha256sum -c" in script


def test_ci_pins_the_uv_version() -> None:
    assert 'version: "0.13.1"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_ci_actions_pinned_by_sha() -> None:
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for ref in re.findall(r"uses: (\S+)", wf.read_text()):
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{wf.name}: {ref}"


def test_report_markdown() -> None:
    r = Report(started="2026-11-08 22:00 UTC")
    r.steps.append(Step("helm test", 3.21, True, "a | b"))
    r.facts["x"] = 1
    md = r.markdown()
    assert "| helm test | 3.2 | ok | a / b |" in md
    assert '"x": 1' in md


def test_wait_for_times_out() -> None:
    assert wait_for("ready", lambda: True, timeout=1) < 1
    with pytest.raises(StepFailed, match="timed out"):
        wait_for("never", lambda: False, timeout=0.2, every=0.05)


def _pod(name: str, hash_: str, exit_code: int | None = None, waiting: str = "") -> dict:  # type: ignore[type-arg]
    status: dict = {}  # type: ignore[type-arg]
    if exit_code is not None:
        status["lastState"] = {"terminated": {"exitCode": exit_code}}
    if waiting:
        status["state"] = {"waiting": {"reason": waiting}}
    return {
        "metadata": {"name": name, "labels": {"pod-template-hash": hash_}},
        "status": {"containerStatuses": [status]},
    }


def test_crash_evidence_counts_only_new_pods() -> None:
    from deploy_reference.e2e import crash_lines, template_hashes

    old = [_pod("old-a", "aaa", exit_code=137)]  # an old pod that was OOM-killed earlier
    assert template_hashes(old) == {"aaa"}
    assert crash_lines(old, {"aaa"}) == []
    new = [_pod("new-b", "bbb", exit_code=1, waiting="CrashLoopBackOff"), _pod("new-c", "bbb")]
    assert crash_lines(old + new, {"aaa"}) == ["new-b: exit 1 CrashLoopBackOff"]


def test_metrics_server_tag_line_is_rewritten_before_apply() -> None:
    e2e = (ROOT / "src" / "deploy_reference" / "e2e.py").read_text()
    assert "body.replace(tag_line" in e2e
    assert '"set", "image"' not in e2e
