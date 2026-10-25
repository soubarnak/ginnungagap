#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-10 (R-003): collect image digests, sizes, config metadata and signature facts for the
# Trust Manifest (src/ginn/trust/data/trust.toml). Network: image registries only (DEP-4).
# Pulls no images. Compressed sizes come from the manifest. Uncompressed sizes come from the
# gzip ISIZE trailer of each layer, read with a 4-byte HTTP range request (ISIZE is the
# uncompressed size modulo 2^32, so a layer above 4 GiB under-reports and is flagged).
#
# usage: spikes/s10_image_channels.sh [registry/repo:tag ...]
#   no arguments: the S-10 candidate set.
#   stdout: a TOML fragment with one [[candidate]] table per reference. stderr: progress.
#   needs: bash 4.4+, skopeo, curl, jq, openssl, coreutils.
set -euo pipefail

CANDIDATES=(
  quay.io/archlinux/archlinux:base
  docker.io/library/archlinux:base
  docker.io/library/debian:stable-slim
  docker.io/library/debian:stable
  registry.fedoraproject.org/fedora:44
  registry.fedoraproject.org/fedora:latest
  registry.fedoraproject.org/fedora:45
  registry.fedoraproject.org/fedora-minimal:44
  docker.io/kalilinux/kali-rolling:latest
  docker.io/kalilinux/kali-last-release:latest
)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

log() { printf '%s\n' "$*" >&2; }
die() { printf 'S-10 error: %s\n' "$*" >&2; exit 1; }

# Sets REG, REPO, TAG and API (the HTTP host; Docker Hub serves its API from registry-1).
parse_ref() {
  REG=${1%%/*}
  local rest=${1#*/}
  TAG=${rest##*:}
  REPO=${rest%:*}
  API=$REG
  if [ "$REG" = docker.io ]; then API=registry-1.docker.io; fi
  return 0
}

# Sets AUTH to a curl header array with an anonymous pull token, or leaves it empty when the
# registry answers /v2/ without a Bearer challenge.
auth_for_repo() {
  AUTH=()
  local challenge realm service token
  challenge=$(curl -sI --max-time 30 "https://$API/v2/" | tr -d '\r' \
    | sed -n 's/^[Ww][Ww][Ww]-[Aa]uthenticate: //p' | sed -n '1p')
  if [ -z "$challenge" ]; then return 0; fi
  realm=$(sed -n 's/.*realm="\([^"]*\)".*/\1/p' <<<"$challenge")
  service=$(sed -n 's/.*service="\([^"]*\)".*/\1/p' <<<"$challenge")
  token=$(curl -sG --max-time 30 "$realm" \
    --data-urlencode "service=$service" --data-urlencode "scope=repository:$REPO:pull" \
    | jq -r '.token // empty')
  [ -n "$token" ] || die "no anonymous pull token for $REG/$REPO"
  AUTH=(-H "Authorization: Bearer $token")
  return 0
}

# Prints the uncompressed size (gzip ISIZE) of the layer with digest $1 and media type $2.
isize_of() {
  case "$2" in
    *tar+gzip|*tar.gzip) ;;
    *) die "$REG/$REPO layer $1 has media type $2: no gzip trailer, refusing to guess" ;;
  esac
  curl -sSL --max-time 120 ${AUTH[@]+"${AUTH[@]}"} -H 'Range: bytes=0-1' \
    -o "$WORK/head.bin" "https://$API/v2/$REPO/blobs/$1"
  curl -sSL --max-time 120 ${AUTH[@]+"${AUTH[@]}"} -H 'Range: bytes=-4' \
    -o "$WORK/tail.bin" "https://$API/v2/$REPO/blobs/$1"
  [ "$(stat -c %s "$WORK/head.bin")" -eq 2 ] && [ "$(stat -c %s "$WORK/tail.bin")" -eq 4 ] \
    || die "range reads of $1 returned the wrong length (server ignored Range?)"
  local h0 h1 t0 t1 t2 t3
  read -r h0 h1 < <(od -An -tu1 -v "$WORK/head.bin")
  [ "$h0" -eq 31 ] && [ "$h1" -eq 139 ] || die "layer $1 does not start with gzip magic 1f 8b"
  read -r t0 t1 t2 t3 < <(od -An -tu1 -v "$WORK/tail.bin")
  echo $((t0 + t1 * 256 + t2 * 65536 + t3 * 16777216))
}

# Checks the cosign-format signature stored at the .sig tag (in $WORK/sigmanifest.json) against
# platform digest $1. Prints one summary line. Checks: the payload blob hash, the payload's
# docker-manifest-digest, and an ECDSA signature over the payload with the key from the bundle.
# It does NOT check the Fulcio chain or Rekor inclusion (no cosign, registries-only network).
cosign_check() {
  local lay pdig sigb64 bundle body pem logidx pinned pin kind ident ecdsa
  lay=$(jq -c '.layers[0]' "$WORK/sigmanifest.json")
  pdig=$(jq -r '.digest' <<<"$lay")
  sigb64=$(jq -r '.annotations["dev.cosignproject.cosign/signature"]' <<<"$lay")
  bundle=$(jq -r '.annotations["dev.sigstore.cosign/bundle"] // empty' <<<"$lay")
  curl -sSL --max-time 120 ${AUTH[@]+"${AUTH[@]}"} -o "$WORK/payload.json" \
    "https://$API/v2/$REPO/blobs/$pdig"
  if [ "$(sha256sum "$WORK/payload.json" | cut -d' ' -f1)" != "${pdig#sha256:}" ]; then
    echo "payload blob hash mismatch"; return 0
  fi
  pinned=$(jq -r '.critical.image["docker-manifest-digest"]' "$WORK/payload.json")
  if [ "$pinned" = "$1" ]; then pin=pinned-digest-ok; else pin="payload-pins-$pinned"; fi
  printf '%s' "$sigb64" | base64 -d > "$WORK/sig.der"
  logidx=none; pem=""
  if [ -n "$bundle" ]; then
    body=$(jq -r '.Payload.body' <<<"$bundle" | base64 -d)
    pem=$(jq -r '.spec.signature.publicKey.content' <<<"$body" | base64 -d)
    logidx=$(jq -r '.Payload.logIndex' <<<"$bundle")
  fi
  printf '%s' "$pem" > "$WORK/key.pem"
  kind=static-key; ident=n/a
  if grep -q 'BEGIN CERTIFICATE' "$WORK/key.pem"; then
    kind=keyless-cert
    openssl x509 -in "$WORK/key.pem" -pubkey -noout > "$WORK/pub.pem"
    ident=$(openssl x509 -in "$WORK/key.pem" -noout -ext subjectAltName \
      | sed -n 's/.*URI://p' | sed -n '1p')
  else
    cp "$WORK/key.pem" "$WORK/pub.pem"
  fi
  if openssl dgst -sha256 -verify "$WORK/pub.pem" -signature "$WORK/sig.der" \
      "$WORK/payload.json" >/dev/null 2>&1; then ecdsa=ok; else ecdsa=FAILED; fi
  echo "cosign $kind ecdsa=$ecdsa $pin identity=$ident rekor_logIndex=$logidx"
}

# Collects one reference and prints its [[candidate]] TOML table.
collect() {
  parse_ref "$1"
  auth_for_repo
  local ref="docker://$REG/$REPO:$TAG"
  local index raw amd amdver attest pm cfg created version title
  index=$(skopeo inspect "$ref" | jq -r '.Digest')
  raw=$(skopeo inspect --raw "$ref")
  if jq -e '.manifests' >/dev/null <<<"$raw"; then
    amd=$(jq -r '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64" and ((.annotations["vnd.docker.reference.type"] // "") != "attestation-manifest"))][0].digest // empty' <<<"$raw")
    amdver=$(jq -r '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64" and ((.annotations["vnd.docker.reference.type"] // "") != "attestation-manifest"))][0].annotations["org.opencontainers.image.version"] // empty' <<<"$raw")
    attest=$(jq '[.manifests[] | select((.annotations["vnd.docker.reference.type"] // "") == "attestation-manifest")] | length' <<<"$raw")
  else
    amd=$index; amdver=""; attest=0
  fi
  [ -n "$amd" ] || die "$1: no linux/amd64 manifest"

  # Platform manifest: layers and sizes. The platform digest is what gets pinned.
  pm=$(skopeo inspect --raw "docker://$REG/$REPO@$amd")
  cfg=$(skopeo inspect --config "docker://$REG/$REPO@$amd")
  created=$(jq -r '.created // empty' <<<"$cfg")
  version=$(jq -r '.config.Labels["org.opencontainers.image.version"] // empty' <<<"$cfg")
  [ -n "$version" ] || version=$amdver
  title=$(jq -r '.config.Labels["org.opencontainers.image.title"] // empty' <<<"$cfg")

  local compressed=0 uncompressed=0 nlayers=0 mt dg sz isz
  while IFS=$'\t' read -r mt dg sz; do
    nlayers=$((nlayers + 1))
    compressed=$((compressed + sz))
    isz=$(isize_of "$dg" "$mt")
    uncompressed=$((uncompressed + isz))
  done < <(jq -r '.layers[] | [.mediaType, .digest, .size] | @tsv' <<<"$pm")
  [ "$uncompressed" -ge "$compressed" ] \
    || die "$1: uncompressed sum below compressed sum (4 GiB wrap or multi-member gzip)"

  # Signature facts: cosign .sig tag, OCI referrers, Fedora-style lookaside, attestations.
  local sigtag sig_http ref_http ref_count ext_http sig_status="none" sig_detail="no sig tag"
  sigtag="${amd//:/-}.sig"
  sig_http=$(curl -s -o "$WORK/sigmanifest.json" -w '%{http_code}' --max-time 60 \
    ${AUTH[@]+"${AUTH[@]}"} -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    "https://$API/v2/$REPO/manifests/$sigtag")
  if [ "$sig_http" = 200 ]; then
    sig_detail=$(cosign_check "$amd")
    case "$sig_detail" in *ecdsa=ok*pinned-digest-ok*) sig_status=sigstore ;; esac
  fi
  ref_http=$(curl -s -o "$WORK/referrers.json" -w '%{http_code}' --max-time 60 \
    ${AUTH[@]+"${AUTH[@]}"} -H 'Accept: application/vnd.oci.image.index.v1+json' \
    "https://$API/v2/$REPO/referrers/$amd")
  ref_count=0
  if [ "$ref_http" = 200 ]; then ref_count=$(jq '.manifests | length' "$WORK/referrers.json"); fi
  ext_http=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
    "https://$API/extensions/v2/$REPO/signatures/$index")

  printf '[[candidate]]\n'
  printf 'ref = "%s"\n' "$1"
  printf 'index_digest = "%s"\n' "$index"
  printf 'platform_digest = "%s"\n' "$amd"
  printf 'created = "%s"\n' "$created"
  printf 'version = "%s"\n' "$version"
  printf 'title = "%s"\n' "$title"
  printf 'layers = %d\n' "$nlayers"
  printf 'compressed_size_bytes = %d\n' "$compressed"
  printf 'uncompressed_size_bytes = %d\n' "$uncompressed"
  printf 'attestation_manifests = %d\n' "$attest"
  printf 'sigstore_tag_http = %s\n' "$sig_http"
  printf 'referrers_http = %s\n' "$ref_http"
  printf 'referrers_count = %d\n' "$ref_count"
  printf 'lookaside_signatures_http = %s\n' "$ext_http"
  printf 'signature_status = "%s"\n' "$sig_status"
  printf 'signature_detail = "%s"\n' "$sig_detail"
}

main() {
  local refs=("$@")
  if [ "${#refs[@]}" -eq 0 ]; then refs=("${CANDIDATES[@]}"); fi
  printf '# S-10 facts from spikes/s10_image_channels.sh, collected %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local r
  for r in "${refs[@]}"; do
    log "collecting $r"
    collect "$r"
    printf '\n'
  done
}

main "$@"
