#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.session import get_session


def fail(message: str, code: int) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def usage() -> NoReturn:
    print(
        """Usage:
  ci_transient_artifacts.sh put <source-path> <relative-key>
  ci_transient_artifacts.sh get <relative-key> <destination-path>
  ci_transient_artifacts.sh get-prefix <relative-prefix> <destination-dir>
  ci_transient_artifacts.sh exists <relative-key>
  ci_transient_artifacts.sh delete-prefix [relative-prefix]""",
        file=sys.stderr,
    )
    raise SystemExit(64)


def normalize_relative_key(value: str) -> str:
    key = value.lstrip("/")
    invalid = (
        not key
        or key in {".", ".."}
        or key.startswith("../")
        or key.endswith("/..")
        or "/../" in key
    )
    if invalid:
        fail(f"Invalid transient artifact key: {key}", 64)
    return key


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        fail(f"{name} is required", 64)
    return value


def object_key_for(prefix: str, run_namespace: str, relative_key: str) -> str:
    return f"{prefix.rstrip('/')}/{run_namespace.lstrip('/')}/{relative_key}"


def make_client():
    session = get_session()
    return session.create_client(
        "s3",
        endpoint_url=env("CI_TRANSIENT_R2_URL", os.environ.get("CLOUDFLARE_R2_URL")),
        region_name=os.environ.get("CLOUDFLARE_R2_REGION", "auto"),
        aws_access_key_id=env(
            "CI_TRANSIENT_R2_ACCESS_KEY_ID",
            os.environ.get("AWS_ACCESS_KEY_ID"),
        ),
        aws_secret_access_key=env(
            "CI_TRANSIENT_R2_SECRET_ACCESS_KEY",
            os.environ.get("AWS_SECRET_ACCESS_KEY"),
        ),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def list_keys(client, bucket: str, prefix: str) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if key:
                keys.append(key)
    return keys


def put_artifact(client, bucket: str, prefix: str, run_namespace: str, args: list[str]) -> int:
    if len(args) != 2:
        usage()
    source_path = Path(args[0])
    if not source_path.exists():
        fail(f"Source path not found: {source_path}", 66)
    if source_path.is_dir():
        fail(f"Directory uploads are not supported: {source_path}", 64)
    relative_key = normalize_relative_key(args[1])
    client.put_object(
        Bucket=bucket,
        Key=object_key_for(prefix, run_namespace, relative_key),
        Body=source_path.read_bytes(),
    )
    return 0


def get_artifact(client, bucket: str, prefix: str, run_namespace: str, args: list[str]) -> int:
    if len(args) != 2:
        usage()
    relative_key = normalize_relative_key(args[0])
    destination_path = Path(args[1])
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    response = client.get_object(
        Bucket=bucket,
        Key=object_key_for(prefix, run_namespace, relative_key),
    )
    destination_path.write_bytes(response["Body"].read())
    return 0


def get_prefix(client, bucket: str, prefix: str, run_namespace: str, args: list[str]) -> int:
    if len(args) != 2:
        usage()
    relative_prefix = normalize_relative_key(args[0]).rstrip("/")
    destination_dir = Path(args[1])
    destination_dir.mkdir(parents=True, exist_ok=True)
    object_prefix = object_key_for(prefix, run_namespace, relative_prefix) + "/"
    object_keys = list_keys(client, bucket, object_prefix)
    if not object_keys:
        fail(f"No transient artifacts found for prefix: {relative_prefix}", 66)
    for object_key in object_keys:
        relative_path = object_key[len(object_prefix) :]
        target_path = destination_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        response = client.get_object(Bucket=bucket, Key=object_key)
        target_path.write_bytes(response["Body"].read())
    return 0


def exists_artifact(client, bucket: str, prefix: str, run_namespace: str, args: list[str]) -> int:
    if len(args) != 1:
        usage()
    relative_key = normalize_relative_key(args[0])
    try:
        client.head_object(
            Bucket=bucket,
            Key=object_key_for(prefix, run_namespace, relative_key),
        )
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return 1
        raise
    return 0


def delete_prefix(client, bucket: str, prefix: str, run_namespace: str, args: list[str]) -> int:
    if len(args) > 1:
        usage()
    relative_prefix = args[0].rstrip("/") if args else ""
    if relative_prefix:
        object_prefix = (
            object_key_for(
                prefix,
                run_namespace,
                normalize_relative_key(relative_prefix),
            )
            + "/"
        )
    else:
        object_prefix = f"{prefix.rstrip('/')}/{run_namespace.lstrip('/')}/"
    for object_key in list_keys(client, bucket, object_prefix):
        client.delete_object(Bucket=bucket, Key=object_key)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        usage()

    operation = sys.argv[1]
    args = sys.argv[2:]

    if os.environ.get("CI_TRANSIENT_ARTIFACTS_ENABLED", "true") != "true":
        fail("Transient CI artifact storage is disabled", 78)

    bucket = env(
        "CI_TRANSIENT_R2_BUCKET",
        os.environ.get("CLOUDFLARE_R2_BUCKET_NAME"),
    )
    prefix = os.environ.get("CI_TRANSIENT_R2_PREFIX", "ci-transient")
    run_namespace = os.environ.get(
        "CI_TRANSIENT_RUN_PREFIX",
        f"{os.environ.get('GITHUB_REPOSITORY', 'local')}/{os.environ.get('GITHUB_RUN_ID', 'manual')}",
    )

    client = make_client()
    handlers = {
        "put": put_artifact,
        "get": get_artifact,
        "get-prefix": get_prefix,
        "exists": exists_artifact,
        "delete-prefix": delete_prefix,
    }
    handler = handlers.get(operation)
    if handler is None:
        usage()
    return handler(client, bucket, prefix, run_namespace, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClientError as exc:
        error = exc.response.get("Error", {})
        message = error.get("Message") or str(exc)
        code = error.get("Code")
        if code == "404" or code == "NoSuchKey":
            fail(message, 66)
        fail(message, 1)
