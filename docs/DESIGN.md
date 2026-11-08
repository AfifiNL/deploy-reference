# Design notes

## Goals, in priority order

1. A release that does not become healthy never replaces one that is; going back is one command
   and is tested, not just documented.
2. Everything that ends up running is pinned: the service's source commit, base images, Python
   packages, CLI tools, CI actions, Helm test images.
3. The whole deployment can be exercised on a laptop at no cost, end to end, and torn down.
4. The cloud part is ready to apply but has never been applied: it is validated and linted only,
   and it says so.
5. Telemetry is part of the deployment, not an afterthought, and needs no change in the service.

## Decisions

**Deploy an existing service unchanged.** The image is the public `cited-answers` repository at a
fixed commit, checked out and verified (`rev-parse HEAD` must equal the pin), plus an
OpenTelemetry layer. No fork, no patch: what runs is what that repository's own CI tested.

**Auto-instrumentation at build time.** `opentelemetry-instrument` wraps the service's start
command; the FastAPI instrumentation produces a span per request. The OpenTelemetry packages are
compiled with hashes *against the service's own lock file* (as constraints), so adding them
cannot move a service dependency, and `--require-hashes --no-deps` makes the install exact. The
image keeps telemetry off (`OTEL_*_EXPORTER=none`) unless the chart points it at a collector.

**A collector in the chart, an exporter in values.** The app talks OTLP to an in-cluster
collector; the collector's exporters are values. The debug exporter is a separate switch
(`debugExporter`) rather than a default map entry, because Helm merges maps: a user adding an
`otlphttp` exporter would otherwise keep printing every span at full detail. The collector
restarts on any configuration change (the checksum covers the rendered configuration). Health
checks and readiness are excluded from tracing.

**Rollouts that never drop capacity, and pods that drain.** `maxUnavailable: 0`, `maxSurge: 1`,
a readiness probe on `/readyz` (corpus loaded, store reachable), a separate startup probe so slow
starts are not killed by the liveness probe, probe timeouts of 3 s (the default 1 s fails under
heavy CPU load). A new pod must be ready before an old one goes, and a terminating pod sleeps
five seconds in `preStop` so endpoints stop routing to it before it exits. The end-to-end run
exercises the second part with a good release under a prober that runs from before the release
until after it, alternating `/readyz` and real questions; the bad-release drill alone could not,
because a release that never becomes ready never terminates an old pod. It is a check, not a
proof: each probe is a new connection, and there is no control run without the pause. The
PodDisruptionBudget uses `maxUnavailable: 1`, which never blocks a node drain (a
`minAvailable: 1` budget with one replica would), and replicas spread over nodes when possible.

**Rollback = Helm's own, tested with a bad release.** `helm upgrade --rollback-on-failure` waits
for the new pods and rolls back to the last deployed revision if they never become ready. The
drill releases a configuration the service rejects at start-up (a setting out of range) and
checks four things: pods of the NEW template crashed and their previous log names the planted
setting (so a release that failed for another reason, such as no room to schedule, or an old
pod that restarted, does not count), the upgrade failed, the history shows "Rollback to N", and
the in-cluster prober saw no failed request. Manual rollback is `helm rollback ca <revision>`.

**HPA on CPU, with a slow scale-down.** The HPA owns the replica count (the Deployment does not
set `replicas` when autoscaling is on, so a `helm upgrade` cannot reset it). Scale-down waits
five minutes to avoid flapping. The end-to-end run puts load on the service and records the CPU
and replica samples.

**Locked-down pods.** Non-root UID, read-only root filesystem with an `emptyDir` for `/tmp`, all
capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp, no service-account
token. The image passed the same restrictions under plain Docker (`--read-only --tmpfs /tmp`)
before the chart existed.

**kind, not a cloud account, for the end-to-end run.** Three nodes (one control plane, two
workers), the image loaded with `kind load` (no registry), metrics-server from its release
manifest (checksum-verified, patched for kind's self-signed kubelet certificates).

**Terraform without modules.** Plain resources (VPC with flow logs, two public and two private
subnets in regular zones, one NAT gateway, IAM roles, EKS with API authentication mode, KMS
envelope encryption of secrets, control-plane logs with retention, upgrade policy STANDARD so
the cluster never drifts into paid extended support, a managed node group from a launch
template with IMDSv2 and a hop limit of 1, encrypted root volumes and cost tags on instances and
volumes, the VPC CNI on its own role through EKS Pod Identity rather than the node role, core
add-ons at the version AWS recommends, a cost budget). Fewer moving parts to review than
community modules, at the price of more lines. The public API endpoint has no default
allow-list and refuses private ranges: opening it must be a deliberate, public value. Not done:
a customer-managed KMS key policy, KMS encryption of the log groups, remote state.

**Tools pinned like dependencies.** `scripts/install-tools.sh` downloads kind, kubectl, helm,
terraform, tflint and kubeconform from their vendors' release URLs and checks each file
against a SHA-256 written in the script; on later runs it re-hashes the installed binary and
reinstalls it if it changed. CI and other machines run the same script, then the same
`make check`. The service commit appears in four places (build script, chart, values, e2e
runner); a test fails if they disagree. The OpenTelemetry requirements record the SHA-256 of
the service lock file they were compiled against, and the build refuses any other lock file
(`make otel-requirements` re-compiles and re-records).

## What is not tested

- The Terraform has never been planned or applied against AWS (no account was used). `terraform
  validate` and tflint check syntax, types, references and known AWS rule violations, not
  whether AWS accepts every combination at apply time.
- The end-to-end run uses kind; cloud load balancers, IAM roles for service accounts, real
  network policies and node autoscaling are not exercised.
- The e2e workflow is not run in GitHub Actions (minutes on private repositories are limited);
  it runs locally with `make e2e`.
