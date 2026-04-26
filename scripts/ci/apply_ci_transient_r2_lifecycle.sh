#!/usr/bin/env bash
set -euo pipefail

bucket="${CI_TRANSIENT_R2_BUCKET:-${CLOUDFLARE_R2_BUCKET_NAME:-}}"
endpoint_url="${CI_TRANSIENT_R2_URL:-${CLOUDFLARE_R2_URL:-}}"
access_key_id="${CI_TRANSIENT_R2_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
secret_access_key="${CI_TRANSIENT_R2_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"

if [[ -z "$bucket" || -z "$endpoint_url" ]]; then
  echo "CI_TRANSIENT_R2_BUCKET and CI_TRANSIENT_R2_URL are required" >&2
  exit 64
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required to configure R2 lifecycle" >&2
  exit 69
fi

if [[ -z "$access_key_id" || -z "$secret_access_key" ]]; then
  echo "CI_TRANSIENT_R2_ACCESS_KEY_ID and CI_TRANSIENT_R2_SECRET_ACCESS_KEY are required" >&2
  exit 64
fi

export AWS_ACCESS_KEY_ID="$access_key_id"
export AWS_SECRET_ACCESS_KEY="$secret_access_key"
export AWS_EC2_METADATA_DISABLED=true
export AWS_REGION="${CLOUDFLARE_R2_REGION:-auto}"
export AWS_DEFAULT_REGION="${CLOUDFLARE_R2_REGION:-auto}"
unset AWS_PROFILE AWS_DEFAULT_PROFILE
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

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$bucket" \
  --lifecycle-configuration file://scripts/ci/ci-transient-r2-lifecycle.json \
  --endpoint-url "$endpoint_url" \
  --region "${CLOUDFLARE_R2_REGION:-auto}"
