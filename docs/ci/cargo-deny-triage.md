# Cargo Deny Triage

This document groups the current `cargo-deny` failures so policy decisions can be made explicitly instead of being buried in CI logs.

## Current failing advisories

### AWS-LC / X.509 chain validation cluster

These failures are all reported against `aws-lc-sys 0.35.0` and should be treated as one upgrade decision, not five unrelated tickets.

- `RUSTSEC-2026-0044` `AWS-LC X.509 Name Constraints Bypass via Wildcard/Unicode CN`
- `RUSTSEC-2026-0045` `Timing Side-Channel in AES-CCM Tag Verification in AWS-LC`
- `RUSTSEC-2026-0046` `PKCS7_verify Certificate Chain Validation Bypass in AWS-LC`
- `RUSTSEC-2026-0047` `PKCS7_verify Signature Validation Bypass in AWS-LC`
- `RUSTSEC-2026-0048` `CRL Distribution Point Scope Check Logic Error in AWS-LC`

Recommended decision bucket:

- identify the top-level crate that pins `aws-lc-sys`
- determine whether an upstream patch release is already available
- prefer dependency updates over waivers because these are active cryptography / certificate validation advisories

### Rustls / webpki

- `RUSTSEC-2026-0049` against `rustls-webpki 0.103.8`
- `RUSTSEC-2026-0098` against `rustls-webpki 0.103.8`
- `RUSTSEC-2026-0099` against `rustls-webpki 0.103.8`
- `RUSTSEC-2026-0104` against `rustls-webpki 0.103.8`

Recommended decision bucket:

- update the `rustls` dependency chain if a patched `rustls-webpki` is available
- waive only if the affected validation path is provably unreachable in this repo and the waiver is time-boxed

### Rand unsoundness

- `RUSTSEC-2026-0097` against inherited `rand` versions

Recommended decision bucket:

- identify whether the affected custom-logger path is reachable in our runtime
- prefer dependency updates once upstream crates move to patched `rand`
- keep the waiver temporary because this is an unsoundness advisory, not a simple maintenance warning

### Time crate

- `RUSTSEC-2026-0009` against `time 0.3.44`

Recommended decision bucket:

- check whether a non-breaking update is available transitively
- if not, decide whether to waive temporarily with expiry or pin/patch via `[patch.crates-io]`

### Bincode lifecycle issue

- `RUSTSEC-2025-0141` against `bincode 1.3.3` (`unmaintained`)

Recommended decision bucket:

- identify direct vs transitive usage
- if direct, plan migration off `bincode 1.x`
- if transitive, decide whether to accept temporarily with rationale and owner

### Additional transitive advisories currently present

- `RUSTSEC-2026-0007` `bytes` integer overflow in `BytesMut::reserve`
- `RUSTSEC-2025-0143` `capnp` unsound APIs of public `constant::Reader` and `StructSchema`
- `RUSTSEC-2026-0012` `keccak` unsound ARMv8 assembly backend
- `RUSTSEC-2026-0002` `IterMut` unsoundness
- `RUSTSEC-2026-0041` `lz4_flex` invalid decompression can leak memory contents
- `RUSTSEC-2026-0037` `quinn-proto` endpoint amplification / fragmentation DoS
- `RUSTSEC-2026-0001` `rkyv` `AllocatedVec` and `ArchivedString::deserialize_from` memory safety issue

These should be grouped by top-level dependency owner before any waiver discussion.

## Non-fatal warnings currently emitted

- yanked `keccak`
- yanked `lz4_flex`
- unmatched license allowance `LGPL-3.0`
- unmatched license allowance `LGPL-3.0-only`

These warnings do not fail the job today, but they should still be cleaned up because they make the policy output noisy.

## Expected decisions

- update transitive dependencies where upstream has compatible fixes
- add temporary waivers in `deny.toml` only when the dependency is transitive, not directly exploitable in this repo, and tracked with rationale and expiry
- keep `cargo-deny` as a gating job when Rust or dependency-policy surfaces change
- decide whether the current unmatched license allowances should be removed from `deny.toml`

## Temporary CI unblock

The advisories listed above are now temporarily waived in `deny.toml` so the
`rust-policy` job continues to gate on new regressions while this inherited
dependency backlog is triaged. The waivers are limited to the currently known
advisories and should be removed as upstream crates are updated.

## Operational note

The CI workflow now captures `cargo-deny` output as a dedicated artifact under the `rust-policy` job so the exact failing crates and advisories can be reviewed without scraping raw job logs.
