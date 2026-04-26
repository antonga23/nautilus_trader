#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 << 'EOF'
Usage:
  ci_transient_artifacts.sh put <source-path> <relative-key>
  ci_transient_artifacts.sh get <relative-key> <destination-path>
  ci_transient_artifacts.sh get-prefix <relative-prefix> <destination-dir>
  ci_transient_artifacts.sh exists <relative-key>
  ci_transient_artifacts.sh delete-prefix [relative-prefix]
EOF
  exit 64
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required" >&2
    exit 64
  fi
}

normalize_relative_key() {
  local key="$1"
  key="${key#/}"
  case "$key" in
    "" | . | .. | ../* | */../* | */..)
      echo "Invalid transient artifact key: ${key}" >&2
      exit 64
      ;;
  esac
  printf '%s' "$key"
}

configure_aws_cli() {
  local access_key_id="${CI_TRANSIENT_R2_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
  local secret_access_key="${CI_TRANSIENT_R2_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"

  if [[ -z "$access_key_id" || -z "$secret_access_key" ]]; then
    echo "CI_TRANSIENT_R2_ACCESS_KEY_ID and CI_TRANSIENT_R2_SECRET_ACCESS_KEY are required" >&2
    exit 64
  fi

  if ! command -v aws > /dev/null 2>&1; then
    echo "aws CLI is required for transient CI artifact storage" >&2
    exit 69
  fi

  unset AWS_PROFILE AWS_DEFAULT_PROFILE
  export AWS_REGION="${CLOUDFLARE_R2_REGION:-auto}"
  export AWS_DEFAULT_REGION="${CLOUDFLARE_R2_REGION:-auto}"
  aws_config_dir="$(mktemp -d)"
  trap 'rm -rf "${aws_config_dir}"' EXIT
  export AWS_CONFIG_FILE="${aws_config_dir}/config"
  export AWS_SHARED_CREDENTIALS_FILE="${aws_config_dir}/credentials"
  {
    echo "[default]"
    echo "aws_access_key_id=${access_key_id}"
    echo "aws_secret_access_key=${secret_access_key}"
  } > "$AWS_SHARED_CREDENTIALS_FILE"
  chmod 600 "$AWS_SHARED_CREDENTIALS_FILE"

  {
    echo "[default]"
    echo "region=${CLOUDFLARE_R2_REGION:-auto}"
    echo "output=json"
    echo "s3 ="
    echo "    signature_version = s3v4"
    echo "    addressing_style = path"
    echo "    payload_signing_enabled = false"
    echo "    use_accelerate_endpoint = false"
    echo "    use_dualstack_endpoint = false"
    echo "    use_fips_endpoint = false"
  } > "$AWS_CONFIG_FILE"
}

bucket="${CI_TRANSIENT_R2_BUCKET:-${CLOUDFLARE_R2_BUCKET_NAME:-}}"
endpoint_url="${CI_TRANSIENT_R2_URL:-${CLOUDFLARE_R2_URL:-}}"
prefix="${CI_TRANSIENT_R2_PREFIX:-ci-transient}"
run_namespace="${CI_TRANSIENT_RUN_PREFIX:-${GITHUB_REPOSITORY:-local}/${GITHUB_RUN_ID:-manual}}"

if [[ $# -lt 1 ]]; then
  usage
fi

operation="$1"
shift

if [[ "${CI_TRANSIENT_ARTIFACTS_ENABLED:-true}" != "true" ]]; then
  echo "Transient CI artifact storage is disabled" >&2
  exit 78
fi

if [[ -z "$bucket" || -z "$endpoint_url" ]]; then
  echo "CI_TRANSIENT_R2_BUCKET and CI_TRANSIENT_R2_URL are required" >&2
  exit 64
fi

configure_aws_cli

base_uri="s3://${bucket}/${prefix%/}/${run_namespace#/}"
aws_args=(
  --endpoint-url "$endpoint_url"
  --region "${CLOUDFLARE_R2_REGION:-auto}"
  --cli-connect-timeout 10
  --cli-read-timeout 120
)

case "$operation" in
  put)
    [[ $# -eq 2 ]] || usage
    source_path="$1"
    relative_key="$(normalize_relative_key "$2")"
    if [[ ! -e "$source_path" ]]; then
      echo "Source path not found: ${source_path}" >&2
      exit 66
    fi
    if [[ -d "$source_path" ]]; then
      aws s3 cp "$source_path" "${base_uri}/${relative_key%/}/" \
        "${aws_args[@]}" --recursive --only-show-errors
    else
      aws s3 cp "$source_path" "${base_uri}/${relative_key}" "${aws_args[@]}" --only-show-errors
    fi
    ;;

  get)
    [[ $# -eq 2 ]] || usage
    relative_key="$(normalize_relative_key "$1")"
    destination_path="$2"
    mkdir -p "$(dirname "$destination_path")"
    aws s3 cp "${base_uri}/${relative_key}" "$destination_path" "${aws_args[@]}" --only-show-errors
    ;;

  get-prefix)
    [[ $# -eq 2 ]] || usage
    relative_prefix="$(normalize_relative_key "$1")"
    destination_dir="$2"
    mkdir -p "$destination_dir"
    aws s3 cp "${base_uri}/${relative_prefix%/}/" "$destination_dir" \
      "${aws_args[@]}" --recursive --only-show-errors
    ;;

  exists)
    [[ $# -eq 1 ]] || usage
    relative_key="$(normalize_relative_key "$1")"
    aws s3 ls "${base_uri}/${relative_key}" "${aws_args[@]}" > /dev/null
    ;;

  delete-prefix)
    [[ $# -le 1 ]] || usage
    relative_prefix="${1:-}"
    if [[ -n "$relative_prefix" ]]; then
      relative_prefix="$(normalize_relative_key "$relative_prefix")"
      aws s3 rm "${base_uri}/${relative_prefix%/}/" "${aws_args[@]}" --recursive --only-show-errors
    else
      aws s3 rm "${base_uri}/" "${aws_args[@]}" --recursive --only-show-errors
    fi
    ;;

  *)
    usage
    ;;
esac
