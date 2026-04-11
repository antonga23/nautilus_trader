# Rust Policy Trigger Surfaces

The `rust-policy` GitHub Actions job only runs when changes touch Rust code or
dependency-policy surfaces that can affect Rust quality or supply-chain checks.

## Trigger paths

- `crates/**`
- `Cargo.toml`
- `Cargo.lock`
- `deny.toml`
- `supply-chain/**`
- `.pre-commit-config.yaml`
- `.pre-commit-hooks/**`
- `rust-toolchain.toml`
- `capnp-version`
- `schema/**`
- `.github/workflows/build.yml`
- `.github/workflows/build-v2.yml`
- `.github/actions/common-setup/action.yml`
- `.github/actions/cargo-tool-install/action.yml`
- `scripts/ci/detect_changed_surfaces.sh`
- `scripts/ci/install-rust.sh`
- `scripts/ci/run_pre_commit_fast.sh`
- `scripts/ci/run_rust_policy.sh`

These paths are matched in
`scripts/ci/detect_changed_surfaces.sh` via the `rust_pattern` expression.

## Non-trigger examples

Changes limited to Python adapters, strategies, or tests outside the paths above
do not run `rust-policy`. Those changes still go through `pre-commit-fast` and
the relevant Python test jobs.
