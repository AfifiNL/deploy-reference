#!/usr/bin/env bash
# Build the service image locally from the public cited-answers repository at a pinned commit.
# The commit is checked after checkout, so a moved branch or tag cannot change what is built.
set -euo pipefail
REPO=${CITED_ANSWERS_REPO:-https://github.com/AfifiNL/cited-answers.git}
SHA=${CITED_ANSWERS_SHA:-1005c3ad2df68805578c7ee7e77e207c8b8a2abb}
TAG=${IMAGE_TAG:-cited-answers:${SHA:0:7}}
CTX=build/context
rm -rf "$CTX"
mkdir -p "$CTX"
git clone --quiet --filter=blob:none "$REPO" "$CTX/service"
git -C "$CTX/service" -c advice.detachedHead=false checkout --quiet "$SHA"
[[ "$(git -C "$CTX/service" rev-parse HEAD)" == "$SHA" ]] || { echo "checkout is not $SHA"; exit 1; }
rm -rf "$CTX/service/.git"
if [[ "${PREPARE_ONLY:-}" == "1" ]]; then
  echo "prepared $CTX/service at $SHA"
  exit 0
fi
# The OpenTelemetry requirements were compiled against one exact service lock file; refuse to
# build against another (run `make otel-requirements` after changing the commit).
RECORDED=$(cat image/otel-requirements.service-lock.sha256)
ACTUAL=$(sha256sum "$CTX/service/uv.lock" | cut -d' ' -f1)
if [[ "$RECORDED" != "$ACTUAL" ]]; then
  echo "image/otel-requirements.txt was compiled for another service lock file;" >&2
  echo "run 'make otel-requirements' (it prepares the context itself) and review the diff" >&2
  exit 1
fi
cp image/otel-requirements.txt "$CTX/"
docker build --quiet -f image/Dockerfile --build-arg SERVICE_REVISION="$SHA" -t "$TAG" "$CTX"
echo "$TAG"
