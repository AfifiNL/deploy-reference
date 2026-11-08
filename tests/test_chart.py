from __future__ import annotations

import subprocess

import pytest

from conftest import CHART, api_deployment, by_kind, need, render

# Kubernetes JSON schemas at a fixed commit, not the moving default branch.
SCHEMAS = (
    "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/"
    "c416d7b0da0751bd8ebe5be4fa4c8e8274e3bbe2/"
    "{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json"
)


def env_of(dep: dict) -> dict[str, str]:  # type: ignore[type-arg]
    return {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_default_render_has_every_piece() -> None:
    kinds = sorted(d["kind"] for d in render())
    assert kinds == sorted(
        [
            "ServiceAccount",
            "Service",
            "Service",
            "Deployment",
            "Deployment",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
            "ConfigMap",
            "Pod",
        ]
    )


def test_api_pod_is_locked_down() -> None:
    spec = api_deployment(render())["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    c = spec["containers"][0]
    assert c["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert c["resources"]["requests"]["cpu"]
    assert c["resources"]["limits"]["memory"]
    assert {v["mountPath"] for v in c["volumeMounts"]} == {"/tmp"}


def test_probes_and_rollout_never_drop_capacity() -> None:
    dep = api_deployment(render())
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert dep["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}


def test_selectors_match_pod_labels() -> None:
    docs = render()
    dep = api_deployment(docs)
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    assert dep["spec"]["selector"]["matchLabels"].items() <= pod_labels.items()
    svc = next(s for s in by_kind(docs, "Service") if s["metadata"]["name"] == "ca-cited-answers")
    assert svc["spec"]["selector"].items() <= pod_labels.items()
    (pdb,) = by_kind(docs, "PodDisruptionBudget")
    assert pdb["spec"]["selector"]["matchLabels"].items() <= pod_labels.items()


def test_autoscaling_on_by_default() -> None:
    docs = render()
    (hpa,) = by_kind(docs, "HorizontalPodAutoscaler")
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 5)
    assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70
    assert "replicas" not in api_deployment(docs)["spec"]  # the HPA owns the replica count


def test_autoscaling_off_sets_replicas() -> None:
    docs = render("--set", "autoscaling.enabled=false", "--set", "replicaCount=3")
    assert by_kind(docs, "HorizontalPodAutoscaler") == []
    assert api_deployment(docs)["spec"]["replicas"] == 3


def test_min_above_max_is_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as info:
        render("--set", "autoscaling.minReplicas=6")
    assert "must not exceed" in info.value.stderr


def test_otel_wiring() -> None:
    docs = render()
    env = env_of(api_deployment(docs))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://ca-cited-answers-otel-collector:4318"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert "service.version=1005c3a" in env["OTEL_RESOURCE_ATTRIBUTES"]
    collector_svc = next(
        s for s in by_kind(docs, "Service") if s["metadata"]["name"].endswith("otel-collector")
    )
    assert {p["port"] for p in collector_svc["spec"]["ports"]} == {4317, 4318}
    (cm,) = by_kind(docs, "ConfigMap")
    assert "exporters: [debug]" in cm["data"]["config.yaml"]


def test_a_real_exporter_replaces_the_debug_exporter() -> None:
    docs = render(
        "--set",
        "otel.collector.debugExporter=false",
        "--set",
        "otel.collector.exporters.otlphttp.endpoint=https://otel.example.com",
    )
    (cm,) = by_kind(docs, "ConfigMap")
    assert "exporters: [otlphttp]" in cm["data"]["config.yaml"]
    assert "debug" not in cm["data"]["config.yaml"]


def test_no_exporter_is_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as info:
        render("--set", "otel.collector.debugExporter=false")
    assert "at least one exporter" in info.value.stderr


def test_extra_env_cannot_override_chart_variables() -> None:
    for key in ("CA_PROVIDER", "OTEL_TRACES_EXPORTER"):
        with pytest.raises(subprocess.CalledProcessError) as info:
            render("--set", f"extraEnv.{key}=x")
        assert "collides" in info.value.stderr


def test_drains_and_spreads() -> None:
    docs = render()
    spec = api_deployment(docs)["spec"]["template"]["spec"]
    c = spec["containers"][0]
    assert c["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "5"]
    assert spec["terminationGracePeriodSeconds"] > 5
    assert all(
        c[p]["timeoutSeconds"] == 3 for p in ("startupProbe", "readinessProbe", "livenessProbe")
    )
    (tsc,) = spec["topologySpreadConstraints"]
    assert tsc["whenUnsatisfiable"] == "ScheduleAnyway"
    (pdb,) = by_kind(docs, "PodDisruptionBudget")
    assert pdb["spec"] == {**pdb["spec"], "maxUnavailable": 1}
    assert "minAvailable" not in pdb["spec"]


def test_otel_off_removes_collector_and_env() -> None:
    docs = render("--set", "otel.enabled=false")
    assert by_kind(docs, "ConfigMap") == []
    assert len(by_kind(docs, "Deployment")) == 1
    assert not any(k.startswith("OTEL_") for k in env_of(api_deployment(docs)))


def test_images_pinned_by_digest() -> None:
    docs = render("--set", "image.digest=sha256:" + "a" * 64)
    images = [
        c["image"]
        for d in docs
        for c in (
            d.get("spec", {})
            .get("template", {})
            .get("spec", d.get("spec", {}))
            .get("containers", [])
        )
    ]
    assert images
    assert all("@sha256:" in i for i in images), images
    assert "cited-answers@sha256:" + "a" * 64 in images


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["-f", str(CHART / "values-kind.yaml")],
        ["--set", "otel.enabled=false", "--set", "autoscaling.enabled=false"],
    ],
)
def test_manifests_validate_against_kubernetes_schemas(values: list[str]) -> None:
    rendered = subprocess.run(
        [need("helm"), "template", "ca", str(CHART), *values],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = subprocess.run(
        [
            need("kubeconform"),
            "-strict",
            "-summary",
            "-kubernetes-version",
            "1.35.0",
            "-schema-location",
            SCHEMAS,
            "-",
        ],
        input=rendered,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Invalid: 0, Errors: 0" in result.stdout
