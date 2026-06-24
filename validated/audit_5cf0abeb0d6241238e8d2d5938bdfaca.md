Audit Report

## Title
Delegation Certificate Timestamp Parsed But Never Validated, Enabling Stale Certificate Replay - (File: `rs/certification/src/lib.rs`)

## Summary

In `verify_delegation_certificate`, the `time` field of the delegation certificate's state tree is deserialized into a `SubnetCertificateData` struct but is explicitly annotated `#[allow(unused)]` with the comment `// currently delegation timestamps are not checked`. The field is required for deserialization to succeed but is never compared against any current-time bound. A malicious boundary node can replay an arbitrarily old delegation certificate — one that may carry a rotated-away subnet public key or stale canister-range information — and `verify_delegation_certificate` will accept it, causing clients to verify the main certificate against a stale subnet key.

## Finding Description

In `rs/certification/src/lib.rs` at lines 373–379, `SubnetCertificateData` is defined inside `verify_delegation_certificate`:

```rust
#[derive(Debug, Deserialize)]
struct SubnetCertificateData {
    #[allow(unused)] // currently delegation timestamps are not checked
    time: Leb128EncodedU64,
    subnet: BTreeMap<SubnetId, SubnetView>,
    canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
}
```

The function (lines 357–480):
1. Verifies the root-subnet threshold signature on the delegation certificate (line 389).
2. Deserializes `SubnetCertificateData` from the tree, requiring `time` to be present (lines 391–398).
3. Checks canister ranges and extracts the subnet public key (lines 409–471).
4. **Never reads `subnet_state.time`** — the field is silently dropped.

`verify_delegation_certificate` accepts no `current_time` parameter, so no staleness bound can be enforced. It is called from `verify_certificate_internal` (line 338), which is called by the public entry points `verify_certified_data`, `verify_certificate`, and `verify_certificate_for_subnet_read_state` (lines 129–317).

The exploit path for the key-rotation scenario:
1. Boundary node captures a legitimately signed delegation certificate issued before a DKG resharing (old subnet public key `K_old`).
2. Boundary node also has an old main certificate signed under `K_old`.
3. Client calls `verify_certified_data`. `verify_delegation_certificate` accepts the old delegation cert (no time check), returns `K_old`.
4. `verify_certificate_signature` verifies the old main cert against `K_old` — succeeds.
5. Client receives the old main certificate's `time` value. If the client does not enforce a freshness bound on the returned `Time`, it accepts stale certified data without any indication that the subnet key has since been rotated.

The canister-migration scenario follows the same path: an old delegation cert with stale canister-range information is accepted, allowing a boundary node to serve a client a certificate for a canister that has since migrated to a different subnet.

The test `should_fail_to_validate_delegation_cert_if_time_missing` (lines 811–839 of `rs/certification/src/tests.rs`) confirms the field must be present for deserialization to succeed, yet no test asserts rejection of a certificate whose `time` is in the distant past — because no such rejection occurs.

## Impact Explanation

This is a **Medium** impact finding: a forged or stale certified response accepted under constrained conditions. The attack requires a malicious or compromised boundary node and captured old certificates. The main certificate's `time` is returned to the caller by `verify_certified_data`, so clients that enforce a freshness bound on the returned `Time` would detect staleness. However, clients that do not check the returned time — or that use `verify_certificate` / `verify_certificate_for_subnet_read_state`, which do not return a time at all — will silently accept stale certified data. In the key-rotation scenario, the stale delegation certificate carries a rotated-away public key, undermining the certification chain's integrity guarantee for those clients.

## Likelihood Explanation

Boundary nodes are explicitly not fully trusted in the IC security model and are reachable by any unprivileged user. Old delegation certificates are publicly observable in any IC response. No private key material, governance majority, or privileged access is required beyond control of a single boundary node. The missing check is explicitly acknowledged in the source code comment, confirming it is a known gap. DKG resharings and canister migrations are routine IC operations, making the preconditions realistic.

## Recommendation

After verifying the delegation certificate's signature, compare `subnet_state.time` against the verifier's current time and reject certificates whose timestamp is older than an acceptable staleness bound:

```rust
let cert_time = Time::from_nanos_since_unix_epoch(subnet_state.time.0);
if cert_time + MAX_DELEGATION_CERTIFICATE_AGE < current_time {
    return Err(CertificateValidationError::StaleDelegationCertificate { cert_time, current_time });
}
```

A `current_time` parameter should be added to `verify_delegation_certificate` and threaded through all call sites. A corresponding `StaleDelegationCertificate` variant should be added to `CertificateValidationError`. A test asserting rejection of a delegation certificate whose `time` is in the distant past should be added alongside `should_fail_to_validate_delegation_cert_if_time_missing`.

## Proof of Concept

1. Obtain any valid IC certificate with a delegation (e.g., from `/api/v2/canister/<id>/read_state`). Extract `delegation.certificate` bytes.
2. Call `verify_delegation_certificate(&old_delegation_bytes, &subnet_id, &root_pk, Some(&canister_id), false)`.
3. Observe the call succeeds regardless of how old `old_delegation_bytes` is — `subnet_state.time` is parsed but never compared against any bound.
4. The existing test `should_fail_to_validate_delegation_cert_if_time_missing` (lines 811–839, `rs/certification/src/tests.rs`) confirms the field must be present, yet no corresponding test asserts rejection of a certificate with a `time` in the distant past, because no such rejection occurs.