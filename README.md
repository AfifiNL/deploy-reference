# deploy-reference

A reference deployment for a Python web service: a container image built from a pinned source
commit, a Helm chart with OpenTelemetry tracing, autoscaling and a tested rollback, an
end-to-end run on a local kind cluster, and Terraform for an EKS cluster that is validated in CI
but has never been applied.

The service is [cited-answers](https://github.com/AfifiNL/cited-answers) (question answering over
the EU AI Act and the GDPR), deployed unchanged in its offline mode: no API keys, no model costs.

- **Image**: the service at a pinned commit (verified after checkout), base images pinned by
  digest, only the source, corpus and lock file copied in, OpenTelemetry auto-instrumentation
  installed with hashes compiled against the service's own lock file. Built locally; pushed to
  no registry.
- **Helm chart**: non-root, read-only root filesystem, no capabilities, no service-account
  token; startup, readiness and liveness probes; rolling updates that never drop below the
  current capacity, with a pre-stop pause so terminating pods drain; soft spreading over nodes;
  a PodDisruptionBudget that never blocks a node drain; a CPU HorizontalPodAutoscaler; an
  in-cluster OpenTelemetry collector; a `helm test` that asks a real question.
- **Releases, good and bad, under a prober**: a normal release rolls every pod while an
  in-cluster prober alternates `/readyz` checks and real questions to `/v1/answer` through the
  Service, from before the release starts until after it ends; a release that crashes on purpose
  is rolled back by `helm upgrade --rollback-on-failure`, under the same prober, and the run
  checks that the new pods crashed on the planted setting (their log names it).
- **kind end-to-end run** (`make e2e`): build, cluster, deploy, test, traces, load, rollback,
  teardown, with a timed report.
- **Terraform for EKS** in [`infra/aws`](infra/aws): VPC, private node group, KMS-encrypted
  secrets, control-plane logs, core add-ons, a cost budget. `fmt`, `validate` and `tflint` pass;
  it has not been planned or applied.
- **Pinned inputs**. By digest or hash: base images, the collector, metrics-server and test
  images, the Python packages, the six CLI tools (kind, kubectl, helm, terraform, tflint,
  kubeconform: SHA-256 of each download written in the script; installed binaries are re-hashed
  against the hash recorded at install), the
  metrics-server manifest (its image is rewritten to a digest before it is applied), the
  Kubernetes schemas (a fixed commit) and every CI action. By exact
  version only: uv in CI, the AWS provider (lock file), the tflint AWS ruleset (tflint checks its
  signature) and the kind node image (kind's default for its release). EKS add-ons resolve to
  the version AWS recommends for the cluster version at plan time.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker, git and curl on Linux x86-64.

```bash
make install    # .venv with the dev tools (locked)
make check      # pinned tools into .tools/bin, lint, types, chart lint + render + schema
                # validation, pin checks, terraform fmt/validate/tflint (what CI runs)
make e2e        # build the image, run everything on a kind cluster, delete the cluster
```

`make e2e` takes a few minutes and needs no cloud account; the report lands in
`reports/e2e-report.md`. `make e2e-keep` leaves the cluster running for exploring:

```bash
kubectl --context kind-deploy-reference -n cited-answers port-forward svc/ca-cited-answers 18081:80
curl -s localhost:18081/v1/answer -H 'content-type: application/json' \
  -d '{"question": "When must a controller notify a personal data breach?"}'
make kind-down
```

## End-to-end run

`make e2e` on 2026-11-08 on one 8-thread desktop machine (kind v0.33.0, Kubernetes v1.37.0,
three nodes). Full report: [docs/e2e-run-2026-11-08.md](docs/e2e-run-2026-11-08.md).

| step | seconds | result |
|---|---|---|
| preflight | 0.0 | local port for the port-forward is free |
| build image (layers cached from an earlier build) | 2.4 | `cited-answers:1005c3a` |
| create kind cluster | 28.5 | 1 control plane + 2 workers |
| load image into kind | 7.5 | no registry involved |
| install metrics-server | 26.4 | v0.9.1, manifest SHA-256 verified, image rewritten to its digest |
| helm install | 6.1 | 2 replicas ready |
| helm test | 6.0 | `/readyz` and a question answered with a citation to GDPR Article 33 |
| answers through the service | 0.6 | via port-forward: GDPR Article 33(2) and 33(1) cited; an out-of-scope question refused |
| traces reach the collector | 0.1 | spans for `POST /v1/answer` in the collector's output |
| autoscaling under load | 152.2 | 6 parallel request loops for 120 s: CPU went to 2,699% of the 100m request; the HPA scaled 2 → 5 and all 5 pods became ready; 0 container restarts |
| zero-downtime rolling update | 33.4 | a good release rolled every pod in 23 s; the in-cluster prober, running from before to after the rollout, made 109 requests (`/readyz` and real questions), 0 failed |
| rollback drill | 104.2 | a pod of the bad release exited with code 1 and its log named the planted setting; the upgrade failed after the 90 s timeout and Helm rolled back to revision 2; the prober made 344 requests during the drill, 0 failed |
| teardown | 1.2 | cluster deleted |

Earlier runs the same evening, before the review fixes, gave the same autoscaling and rollback
outcomes.

What the two release steps show together: a good release replaces every pod without a failed
request (new pods must be ready before old ones go, and terminating pods keep serving for five
seconds while endpoints update); a bad release never replaces a ready pod, Helm records it as
failed and rolls back, and the service keeps answering throughout.

## How it fits together

```text
cited-answers @ pinned commit ──► image/Dockerfile ──► cited-answers:1005c3a (local)
   (public repo, checked out        + OpenTelemetry        │  kind load (no registry)
    and verified by SHA)              (hash-pinned)          ▼
                                                   ┌─────────── kind cluster ───────────┐
charts/cited-answers ── helm upgrade --install ──► │ Deployment (2..5 pods) ◄── HPA      │
   values.yaml          --rollback-on-failure      │   │ OTLP/HTTP          metrics-server│
   values-kind.yaml                                │   ▼                                 │
                                                   │ otel-collector ── debug exporter    │
                                                   │ Service ◄── helm test, prober, load │
                                                   └─────────────────────────────────────┘
infra/aws (Terraform) ── validate + tflint only ──► EKS: the same chart with values.yaml
```

Rolling back by hand:

```bash
helm -n cited-answers history ca
helm -n cited-answers rollback ca <revision> --wait
```

Design decisions and their reasons are in [docs/DESIGN.md](docs/DESIGN.md).

## Limits

- **The Terraform has never been applied.** It passes `terraform validate` and tflint with the
  AWS ruleset; AWS may still reject a combination at apply time. See
  [infra/aws/README.md](infra/aws/README.md) before spending money.
- **kind is not EKS**: no cloud load balancer, no IAM roles for service accounts, no node
  autoscaling. The HPA scales pods, not nodes.
- **The service runs in its offline mode** (deterministic mock model). Its live-model mode needs
  provider credentials, which this deployment deliberately does not handle; a real setup would
  add a Secret and an external secrets operator.
- **The end-to-end run is local only.** CI runs `make check` (static and rendered checks); the
  kind run and the image build are not executed in GitHub Actions. `make e2e` refuses to touch
  an existing cluster of the same name unless told to (`--reuse`, never deleted).
- **Traces go to a debug exporter** in the collector's log by default. For a real backend set
  `otel.collector.debugExporter: false` and an `otel.collector.exporters` entry (the chart
  refuses a collector without any exporter).
- **No NetworkPolicy** in the chart; add one for your cluster's ingress setup.
- Linux x86-64 only for the tool installer.

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
