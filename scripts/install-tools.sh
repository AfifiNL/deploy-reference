#!/usr/bin/env bash
# Install the pinned CLI tools into a directory (default: ./.tools/bin), from the vendors' own
# release URLs, each verified against a SHA-256 pinned here. Re-running is cheap: a tool is
# skipped only when its stamp names the pinned download AND the installed binary still hashes
# to what was installed from it. Linux x86-64 only.
set -euo pipefail

DEST=${1:-"$(pwd)/.tools/bin"}
mkdir -p "$DEST"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# name | version | url | sha256 of the downloaded file | path inside the archive ("-" = plain binary)
TOOLS=(
  "kind|v0.33.0|https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64|aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d|-"
  "kubectl|v1.37.2|https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl|c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4|-"
  "helm|v4.3.1|https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz|0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b|linux-amd64/helm"
  "terraform|1.17.1|https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_linux_amd64.zip|4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061|terraform"
  "tflint|v0.66.0|https://github.com/terraform-linters/tflint/releases/download/v0.66.0/tflint_linux_amd64.zip|22be11cadc25cd02401e94c0ccc91aa63467debb8485c2a755d98f4c0a9d1e2e|tflint"
  "kubeconform|v0.8.0|https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz|9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883|kubeconform"
)

for entry in "${TOOLS[@]}"; do
  IFS='|' read -r name version url sha inner <<<"$entry"
  stamp="$DEST/.$name.sha256"
  if [[ -x "$DEST/$name" && -f "$stamp" ]]; then
    read -r pinned installed <"$stamp" || true
    if [[ "$pinned" == "$sha" && "$installed" == "$(sha256sum "$DEST/$name" | cut -d' ' -f1)" ]]; then
      continue
    fi
  fi
  file="$WORK/$(basename "$url")"
  curl -sSfL --retry 3 -o "$file" "$url"
  echo "$sha  $file" | sha256sum -c --quiet - || { echo "checksum mismatch for $name"; exit 1; }
  case "$inner" in
    -) install -m 0755 "$file" "$DEST/$name" ;;
    *)
      mkdir -p "$WORK/$name"
      if [[ "$file" == *.zip ]]; then
        python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract(sys.argv[2], sys.argv[3])' \
          "$file" "$inner" "$WORK/$name"
      else
        tar -xzf "$file" -C "$WORK/$name" "$inner"
      fi
      install -m 0755 "$WORK/$name/$inner" "$DEST/$name"
      ;;
  esac
  echo "$sha $(sha256sum "$DEST/$name" | cut -d' ' -f1)" >"$stamp"
  echo "installed $name $version"
done
