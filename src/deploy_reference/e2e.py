"""End-to-end run on a local kind cluster: build, deploy, test, observe, load, roll back, clean up.

Every step is timed and recorded; the report (Markdown + JSON) is what the README quotes. Nothing
leaves the machine: the image is loaded into kind, never pushed, and the cluster is deleted at the
end unless --keep is given.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "cited-answers"
KIND_CONFIG = ROOT / "kind" / "cluster.yaml"
CLUSTER = "deploy-reference"
NAMESPACE = "cited-answers"
RELEASE = "ca"
FULLNAME = f"{RELEASE}-cited-answers"
IMAGE = "cited-answers:1005c3a"
PORT = 18081  # local port-forward; checked to be free before use
METRICS_SERVER_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.1/components.yaml"
)
METRICS_SERVER_SHA256 = "3c7f57cdbee0f57fb0259a150e8f6d6cb4f29d72d1aa5dce8803151dd6d4c2a1"
# The manifest names its image by tag; the run replaces it with the digest of that tag.
METRICS_SERVER_IMAGE = (
    "registry.k8s.io/metrics-server/metrics-server@"
    "sha256:9b0ba1926e1d3089673f45461c3c1416109dea33b3ba3434048defd380df329b"
)
CURL_IMAGE = (
    "curlimages/curl@sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93"
)
QUESTION = "When must a controller notify a personal data breach?"
# A release that fails at start-up (the app rejects the setting): the rollback drill's bad release.
BAD_RELEASE = ["--set", "extraEnv.CA_MIN_EVIDENCE_COVERAGE=2"]


class StepFailed(RuntimeError):
    pass


@dataclass
class Step:
    name: str
    seconds: float
    ok: bool
    detail: str = ""


@dataclass
class Report:
    started: str
    steps: list[Step] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def markdown(self) -> str:
        lines = [
            f"# End-to-end run, {self.started}",
            "",
            "| step | seconds | result | detail |",
            "|---|---|---|---|",
        ]
        for s in self.steps:
            detail = s.detail.replace("|", "/").replace("\n", " ")[:320]
            lines.append(
                f"| {s.name} | {s.seconds:.1f} | {'ok' if s.ok else 'FAILED'} | {detail} |"
            )
        lines += ["", "## Facts", "", "```json", json.dumps(self.facts, indent=2), "```", ""]
        return "\n".join(lines)


def run(cmd: list[str], *, check: bool = True, timeout: float = 600) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and p.returncode != 0:
        raise StepFailed(f"{' '.join(cmd[:4])}… exited {p.returncode}: {p.stderr.strip()[-400:]}")
    return p.stdout


def kubectl(*args: str, check: bool = True, timeout: float = 300) -> str:
    return run(
        ["kubectl", "--context", f"kind-{CLUSTER}", "-n", NAMESPACE, *args],
        check=check,
        timeout=timeout,
    )


def helm(*args: str, check: bool = True, timeout: float = 600) -> str:
    return run(
        ["helm", "--kube-context", f"kind-{CLUSTER}", "-n", NAMESPACE, *args],
        check=check,
        timeout=timeout,
    )


def wait_for(what: str, probe: Callable[[], bool], timeout: float, every: float = 3) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if probe():
            return time.monotonic() - start
        time.sleep(every)
    raise StepFailed(f"timed out after {timeout:.0f}s waiting for {what}")


def port_free(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                if sock.connect_ex((host, port)) == 0:
                    return False
        except OSError:
            continue  # no IPv6 on this machine
    return True


@contextmanager
def port_forward() -> Iterator[str]:
    if not port_free(PORT):
        raise StepFailed(f"local port {PORT} is in use; free it and run again")
    proc = subprocess.Popen(
        [
            "kubectl",
            "--context",
            f"kind-{CLUSTER}",
            "-n",
            NAMESPACE,
            "port-forward",
            f"svc/{FULLNAME}",
            f"{PORT}:80",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{PORT}"
    try:
        wait_for("port-forward", lambda: _get(f"{base}/healthz") is not None, 30, every=0.5)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _get(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - local port-forward
            data: dict[str, Any] = json.loads(r.read())
            return data
    except OSError:
        return None


def ask(base: str, question: str) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 - local port-forward
        f"{base}/v1/answer",
        data=json.dumps({"question": question}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        data: dict[str, Any] = json.loads(r.read())
        return data


def pod_phase(name: str) -> str:
    return kubectl("get", "pod", name, "-o", "jsonpath={.status.phase}", check=False)


def start_prober() -> None:
    """An in-cluster pod that, until told to stop, asks the Service for /readyz and posts a real
    question to /v1/answer, alternately, about four times a second. Stop it with stop_prober()."""
    body = json.dumps({"question": QUESTION}).replace('"', '\\"')
    script = (
        "ok=0; fail=0; n=0; end=$(( $(date +%s) + 600 ));"
        " while [ ! -f /tmp/stop ] && [ $(date +%s) -lt $end ]; do"
        " if [ $((n % 2)) -eq 0 ]; then"
        f" curl -fsS -m 3 -o /dev/null http://{FULLNAME}/readyz;"
        " else"
        f' curl -fsS -m 10 -o /dev/null -X POST -H "content-type: application/json"'
        f' -d "{body}" http://{FULLNAME}/v1/answer;'
        " fi && ok=$((ok+1)) || fail=$((fail+1)); n=$((n+1)); sleep 0.25; done;"
        " echo ok=$ok fail=$fail"
    )
    kubectl("delete", "pod", "prober", "--ignore-not-found", "--wait=true")
    overrides = json.dumps(
        {
            "spec": {
                "containers": [
                    {"name": "prober", "image": CURL_IMAGE, "command": ["sh", "-c", script]}
                ]
            }
        }
    )
    kubectl("run", "prober", "--image", CURL_IMAGE, "--restart=Never", "--overrides", overrides)
    wait_for("prober", lambda: pod_phase("prober") == "Running", 60, 2)
    time.sleep(3)  # a few probes before the action starts


def stop_prober(timeout: float = 60) -> tuple[int, int, str]:
    """Signal the prober to stop and return (ok, failed, its summary line)."""
    time.sleep(3)  # a few probes after the action ends
    kubectl("exec", "prober", "--", "touch", "/tmp/stop")  # noqa: S108 - inside the probe pod
    wait_for("prober result", lambda: pod_phase("prober") == "Succeeded", timeout, 2)
    out = kubectl("logs", "prober").strip()
    kubectl("delete", "pod", "prober", "--wait=false")
    counts = dict(part.split("=", 1) for part in out.split() if "=" in part)
    return int(counts.get("ok", "0")), int(counts.get("fail", "-1")), out


def template_hashes(pods: list[dict[str, Any]]) -> set[str]:
    return {p["metadata"].get("labels", {}).get("pod-template-hash", "") for p in pods}


def crash_lines(pods: list[dict[str, Any]], old_hashes: set[str]) -> list[str]:
    """Containers of NEW pods (a template hash not seen before the release) that exited with a
    non-zero code or are in CrashLoopBackOff. Old pods never count."""
    found = []
    for p in pods:
        if p["metadata"].get("labels", {}).get("pod-template-hash", "") in old_hashes:
            continue
        for c in p.get("status", {}).get("containerStatuses", []):
            term = (
                c.get("lastState", {}).get("terminated")
                or c.get("state", {}).get("terminated")
                or {}
            )
            waiting = c.get("state", {}).get("waiting", {}).get("reason", "")
            if term.get("exitCode", 0) != 0 or waiting == "CrashLoopBackOff":
                found.append(
                    f"{p['metadata']['name']}: exit {term.get('exitCode')} {waiting}".strip()
                )
    return found


def api_pods() -> list[dict[str, Any]]:
    out = kubectl("get", "pods", "-l", "app.kubernetes.io/component=api", "-o", "json")
    pods: list[dict[str, Any]] = json.loads(out)["items"]
    return pods


def restarts(pods: list[dict[str, Any]]) -> int:
    return sum(
        c.get("restartCount", 0)
        for p in pods
        for c in p.get("status", {}).get("containerStatuses", [])
    )


class Runner:
    def __init__(
        self, keep: bool, skip_build: bool, load_seconds: int, reuse: bool = False
    ) -> None:
        self.keep = keep
        self.skip_build = skip_build
        self.load_seconds = load_seconds
        self.reuse = reuse
        self.created_cluster = False
        self.report = Report(started=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"))

    def step(self, name: str, fn: Callable[[], str]) -> None:
        t0 = time.monotonic()
        try:
            detail = fn()
        except Exception as exc:
            self.report.steps.append(Step(name, time.monotonic() - t0, False, str(exc)))
            raise
        self.report.steps.append(Step(name, time.monotonic() - t0, True, detail))
        print(f"  ok  {name} ({time.monotonic() - t0:.1f}s) {detail}", flush=True)

    # ── steps ────────────────────────────────────────────────────────────────
    def preflight(self) -> str:
        if not port_free(PORT):
            raise StepFailed(f"local port {PORT} is in use; free it and run again")
        return f"port {PORT} free"

    def build(self) -> str:
        if self.skip_build:
            run(["docker", "image", "inspect", IMAGE])
            return f"reused {IMAGE}"
        out = run([str(ROOT / "scripts" / "build-image.sh")], timeout=1200).strip().splitlines()
        return out[-1] if out else IMAGE

    def cluster(self) -> str:
        if CLUSTER in run(["kind", "get", "clusters"]).split():
            if not self.reuse:
                raise StepFailed(
                    f"a kind cluster named {CLUSTER} already exists; delete it "
                    "(make kind-down) or pass --reuse"
                )
            return "reused existing cluster (it will be kept)"
        self.created_cluster = True  # set first: a failed create can still leave a cluster
        run(
            ["kind", "create", "cluster", "--config", str(KIND_CONFIG), "--wait", "120s"],
            timeout=600,
        )
        nodes = run(
            [
                "kubectl",
                "--context",
                f"kind-{CLUSTER}",
                "get",
                "nodes",
                "-o",
                "jsonpath={.items[*].status.nodeInfo.kubeletVersion}",
            ]
        )
        self.report.facts["kubernetes"] = nodes.split()[0] if nodes else "?"
        self.report.facts["nodes"] = len(nodes.split())
        return f"kubelet {nodes}"

    def load_image(self) -> str:
        run(["kind", "load", "docker-image", IMAGE, "--name", CLUSTER], timeout=600)
        return f"{IMAGE} loaded into the nodes (no registry)"

    def metrics_server(self) -> str:
        ctx = ["kubectl", "--context", f"kind-{CLUSTER}"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "components.yaml"
            with urllib.request.urlopen(METRICS_SERVER_URL, timeout=60) as r:
                body = r.read()
            if hashlib.sha256(body).hexdigest() != METRICS_SERVER_SHA256:
                raise StepFailed("metrics-server manifest checksum mismatch")
            tag_line = b"image: registry.k8s.io/metrics-server/metrics-server:v0.9.1"
            if body.count(tag_line) != 1:
                raise StepFailed("metrics-server manifest does not name the expected image")
            path.write_bytes(body.replace(tag_line, b"image: " + METRICS_SERVER_IMAGE.encode()))
            run([*ctx, "apply", "-f", str(path)])
        # kind's kubelets use self-signed serving certificates.
        patch = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/args/-",
                    "value": "--kubelet-insecure-tls",
                }
            ]
        )
        run(
            [
                *ctx,
                "-n",
                "kube-system",
                "patch",
                "deployment",
                "metrics-server",
                "--type=json",
                "-p",
                patch,
            ]
        )
        run(
            [
                *ctx,
                "-n",
                "kube-system",
                "set",
                "image",
                "deployment/metrics-server",
                f"metrics-server={METRICS_SERVER_IMAGE}",
            ]
        )
        run(
            [
                *ctx,
                "-n",
                "kube-system",
                "rollout",
                "status",
                "deployment/metrics-server",
                "--timeout=180s",
            ],
            timeout=240,
        )
        return "v0.9.1, manifest SHA-256 verified, image pinned by digest"

    def install(self) -> str:
        ns = run(
            [
                "kubectl",
                "--context",
                f"kind-{CLUSTER}",
                "create",
                "namespace",
                NAMESPACE,
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        subprocess.run(
            ["kubectl", "--context", f"kind-{CLUSTER}", "apply", "-f", "-"],
            input=ns,
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
        helm(
            "upgrade",
            "--install",
            RELEASE,
            str(CHART),
            "-f",
            str(CHART / "values-kind.yaml"),
            "--wait",
            "--timeout",
            "5m",
            timeout=420,
        )
        ready = kubectl("get", "deploy", FULLNAME, "-o", "jsonpath={.status.readyReplicas}")
        self.report.facts["ready_replicas_after_install"] = int(ready or 0)
        return f"{ready} replicas ready"

    def helm_test(self) -> str:
        out = helm("test", RELEASE, "--logs", "--timeout", "3m", timeout=240)
        if "answered with a citation to Article 33" not in out:
            raise StepFailed("helm test did not report the expected answer")
        return "helm test passed: /readyz and a cited answer"

    def answers(self) -> str:
        with port_forward() as base:
            a = ask(base, QUESTION)
            r = ask(base, "What is the corporate tax rate in the Netherlands?")
        cites = [c.get("label", "") for c in a.get("citations", [])]
        if a.get("status") != "answered" or not any("Article 33" in c for c in cites):
            raise StepFailed(f"unexpected answer: {a.get('status')} {cites}")
        if r.get("status") != "refused":
            raise StepFailed(f"out-of-scope question was not refused: {r.get('status')}")
        self.report.facts["answer_citations"] = cites
        return f"answered with {cites}; out-of-scope question refused (via port-forward)"

    def telemetry(self) -> str:
        collector = f"deploy/{FULLNAME}-otel-collector"

        def seen() -> bool:
            logs = kubectl("logs", collector, "--tail=-1", check=False)
            return "POST /v1/answer" in logs and "cited-answers" in logs

        waited = wait_for("traces in the collector", seen, 90, every=5)
        logs = kubectl("logs", collector, "--tail=-1")
        spans = logs.count("POST /v1/answer")
        self.report.facts["collector_spans_for_answer_route"] = spans
        return f"collector printed {spans} span lines for POST /v1/answer after {waited:.0f}s"

    def autoscaling(self) -> str:
        def has_metrics() -> bool:
            out = kubectl("get", "hpa", FULLNAME, "-o", "json", check=False)
            return bool(out) and bool(json.loads(out).get("status", {}).get("currentMetrics"))

        wait_for("HPA metrics", has_metrics, 240, every=10)
        body = json.dumps({"question": QUESTION}).replace('"', '\\"')
        loop = (
            f'while true; do curl -s -o /dev/null -m 10 -X POST -H "content-type: '
            f'application/json" -d "{body}" http://{FULLNAME}/v1/answer; done'
        )
        script = (
            f"for i in 1 2 3 4 5 6; do ({loop}) & done; sleep {self.load_seconds}; "
            "echo load-finished"
        )
        kubectl("delete", "pod", "load", "--ignore-not-found", "--wait=true")
        overrides = json.dumps(
            {
                "spec": {
                    "containers": [
                        {"name": "load", "image": CURL_IMAGE, "command": ["sh", "-c", script]}
                    ]
                }
            }
        )
        kubectl("run", "load", "--image", CURL_IMAGE, "--restart=Never", "--overrides", overrides)
        samples: list[dict[str, Any]] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.load_seconds + 20:
            st = json.loads(kubectl("get", "hpa", FULLNAME, "-o", "json")).get("status", {})
            cpu = next(
                (
                    m["resource"]["current"].get("averageUtilization")
                    for m in st.get("currentMetrics", [])
                    if m.get("resource")
                ),
                None,
            )
            ready = kubectl("get", "deploy", FULLNAME, "-o", "jsonpath={.status.readyReplicas}")
            samples.append(
                {
                    "t": round(time.monotonic() - t0),
                    "cpu_pct": cpu,
                    "replicas": st.get("currentReplicas"),
                    "desired": st.get("desiredReplicas"),
                    "ready": int(ready or 0),
                }
            )
            time.sleep(10)
        kubectl("delete", "pod", "load", "--wait=false")
        peak = max((s["desired"] or 0) for s in samples)
        peak_ready = max((s["ready"] or 0) for s in samples)
        peak_cpu = max((s["cpu_pct"] or 0) for s in samples)
        restarted = restarts(api_pods())
        self.report.facts["hpa_samples"] = samples
        self.report.facts["hpa_peak_desired_replicas"] = peak
        self.report.facts["peak_ready_replicas"] = peak_ready
        self.report.facts["container_restarts_after_load"] = restarted
        if peak <= 2 or peak_ready <= 2:
            raise StepFailed(f"no scale-out: desired {peak}, ready {peak_ready}, CPU {peak_cpu}%")
        if restarted:
            raise StepFailed(f"{restarted} container restart(s) under load")
        return (
            f"{self.load_seconds}s of load: CPU peaked at {peak_cpu}%, HPA scaled to {peak}, "
            f"{peak_ready} pods ready, 0 restarts"
        )

    def rolling_update(self) -> str:
        """A good release (only a pod annotation changes) rolls every pod while an in-cluster
        prober asks the Service for /readyz: proves the update itself drops no requests."""
        start_prober()
        t0 = time.monotonic()
        helm(
            "upgrade",
            RELEASE,
            str(CHART),
            "-f",
            str(CHART / "values-kind.yaml"),
            "--set-string",  # annotation values must be strings
            f"podAnnotations.rollout={int(time.time())}",
            "--wait",
            "--timeout",
            "5m",
            timeout=420,
        )
        took = time.monotonic() - t0
        ok, fail, out = stop_prober()
        self.report.facts["rolling_update"] = {"seconds": round(took, 1), "probe": out}
        if ok == 0 or fail != 0:
            raise StepFailed(f"requests failed during a good rolling update: {out}")
        return (
            f"good release rolled out in {took:.0f}s; in-cluster prober (readyz + answers, "
            f"running for the whole rollout): {out}"
        )

    def rollback(self) -> str:
        before = json.loads(helm("history", RELEASE, "-o", "json"))
        last_ok = max(r["revision"] for r in before if r["status"] == "deployed")
        old_hashes = template_hashes(api_pods())
        start_prober()
        crash_evidence: list[str] = []
        crash_logs: list[str] = []
        stop = threading.Event()

        def watch() -> None:  # proof that the NEW pods fail for the reason that was planted
            while not stop.is_set():
                try:
                    pods = api_pods()
                    found = crash_lines(pods, old_hashes)
                    crash_evidence.extend(found)
                    if found and not crash_logs:
                        name = found[0].split(":", 1)[0]
                        crash_logs.append(kubectl("logs", name, "--previous", check=False))
                except Exception as exc:  # a kubectl hiccup must not end the watch
                    crash_evidence.append(f"watch error: {type(exc).__name__}")
                stop.wait(3)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        t0 = time.monotonic()
        failed = subprocess.run(
            [
                "helm",
                "--kube-context",
                f"kind-{CLUSTER}",
                "-n",
                NAMESPACE,
                "upgrade",
                RELEASE,
                str(CHART),
                "-f",
                str(CHART / "values-kind.yaml"),
                *BAD_RELEASE,
                "--rollback-on-failure",
                "--timeout",
                "90s",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        drill_s = time.monotonic() - t0
        stop.set()
        watcher.join(timeout=10)
        if failed.returncode == 0:
            raise StepFailed("the bad release was accepted; the drill proves nothing")
        real = [c for c in crash_evidence if not c.startswith("watch error")]
        if not real:
            raise StepFailed("the release failed, but its new pods did not crash")
        if not any("min_evidence_coverage" in log for log in crash_logs):
            raise StepFailed("the new pods crashed, but not on the planted setting")
        after = json.loads(helm("history", RELEASE, "-o", "json"))
        current = max(after, key=lambda r: r["revision"])
        if current["status"] != "deployed" or f"Rollback to {last_ok}" not in current.get(
            "description", ""
        ):
            raise StepFailed(f"no automatic rollback: {current}")
        ok, fail, probe = stop_prober()
        if ok == 0 or fail != 0:
            raise StepFailed(f"requests failed during the drill: {probe}")
        with port_forward() as base:
            a = ask(base, QUESTION)
        evidence = sorted(set(real))
        self.report.facts["rollback"] = {
            "bad_release_rejected_after_s": round(drill_s, 1),
            "crash_evidence": evidence[:3],
            "history": [{k: r[k] for k in ("revision", "status", "description")} for r in after],
            "readyz_probe_during_drill": probe,
            "answer_after_rollback": a.get("status"),
        }
        return (
            f"new pod crashed on the planted setting ({evidence[0]}); upgrade failed after "
            f"{drill_s:.0f}s and was rolled back to revision {last_ok}; in-cluster prober "
            f"(readyz + answers) during the drill: {probe}"
        )

    def teardown(self) -> str:
        if self.keep:
            return "cluster kept (--keep)"
        if not self.created_cluster:
            return "cluster not created by this run: left alone"
        run(["kind", "delete", "cluster", "--name", CLUSTER], timeout=300)
        return "cluster deleted"

    def go(self, out_dir: Path) -> int:
        steps: list[tuple[str, Callable[[], str]]] = [
            ("preflight", self.preflight),
            ("build image", self.build),
            ("create kind cluster", self.cluster),
            ("load image into kind", self.load_image),
            ("install metrics-server", self.metrics_server),
            ("helm install", self.install),
            ("helm test", self.helm_test),
            ("answers through the service", self.answers),
            ("traces reach the collector", self.telemetry),
            ("autoscaling under load", self.autoscaling),
            ("zero-downtime rolling update", self.rolling_update),
            ("rollback drill", self.rollback),
        ]
        code = 0
        try:
            for name, fn in steps:
                self.step(name, fn)
        except Exception as exc:
            print(f"FAILED: {exc}", flush=True)
            code = 1
        finally:
            try:
                self.step("teardown", self.teardown)
            except Exception as exc:
                print(f"teardown failed: {exc}", flush=True)
                code = 1
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "e2e-report.md").write_text(self.report.markdown(), encoding="utf-8")
            (out_dir / "e2e-report.json").write_text(
                json.dumps(
                    {
                        "started": self.report.started,
                        "steps": [s.__dict__ for s in self.report.steps],
                        "facts": self.report.facts,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"report: {out_dir / 'e2e-report.md'}")
        return code
