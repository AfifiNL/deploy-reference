# `make check` is exactly what CI runs: it installs the pinned tools (checksum-verified), lints
# and type-checks the Python, lints and renders the Helm chart and validates it against the
# Kubernetes schemas, and formats, validates and lints the Terraform. Nothing here creates
# cloud resources; `make e2e` creates (and deletes) a local kind cluster.
UV    ?= uv
TOOLS := $(CURDIR)/.tools/bin
export PATH := $(TOOLS):$(PATH)
TF    := infra/aws

.PHONY: help install tools lint fmt typecheck chart test terraform check image e2e e2e-keep \
        kind-down otel-requirements

help:            ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

install:         ## .venv with dev tools (locked)
	$(UV) sync

tools:           ## kind, kubectl, helm, terraform, tflint, kubeconform into .tools/bin (pinned)
	scripts/install-tools.sh $(TOOLS)

lint:            ## ruff lint + format check
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt:             ## apply ruff and terraform formatting
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests
	terraform fmt -recursive infra

typecheck:       ## mypy (strict)
	$(UV) run mypy

chart:           ## helm lint (strict) with the default and the kind values
	helm lint --strict charts/cited-answers
	helm lint --strict charts/cited-answers -f charts/cited-answers/values-kind.yaml

test:            ## pytest: renders the chart, validates it with kubeconform, checks every pin
	$(UV) run pytest

terraform:       ## terraform fmt + validate + tflint (no cloud credentials, nothing applied)
	terraform fmt -recursive -check infra
	terraform -chdir=$(TF) init -backend=false -input=false -lockfile=readonly
	terraform -chdir=$(TF) validate
	cd $(TF) && tflint --init && tflint

check: tools lint typecheck chart test terraform  ## everything CI runs

image:           ## build the service image locally from the pinned public commit
	scripts/build-image.sh

otel-requirements: ## recompile image/otel-requirements.txt against the pinned service lock file
	PREPARE_ONLY=1 scripts/build-image.sh
	cd build/context/service && $(UV) export --frozen --no-dev --no-hashes --no-emit-project -q \
	  > ../../service-constraints.txt
	$(UV) pip compile image/otel.in -c build/service-constraints.txt --python-version 3.12 \
	  --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header -q \
	  -o image/otel-requirements.txt
	sha256sum build/context/service/uv.lock | cut -d' ' -f1 > image/otel-requirements.service-lock.sha256

e2e:             ## build, deploy, test, load, roll back on kind; deletes the cluster at the end
	$(UV) run deploy-reference e2e

e2e-keep:        ## the same, but keep the cluster for exploring
	$(UV) run deploy-reference e2e --keep

kind-down:       ## delete the local cluster
	kind delete cluster --name deploy-reference
