### Title
Delegation Certificate Timestamp Parsed But Never Validated, Enabling Stale Certificate Replay - (File: `rs/certification/src/lib.rs`)

### Summary

In `verify_delegation_certificate`, the `time` field of the delegation certificate is deserialized into a local struct field explicitly annotated `#[allow(unused)]` with the comment `// currently delegation timestamps are not checked`. The field is required for deserialization to succeed but is never compared against any current-time bound. This is the direct IC analog of the external report's pattern: a value is computed/parsed, the constraint that enforces its validity is absent, and the gadget therefore accepts inputs it should reject.

### Finding Description

Inside `verify_delegation_certificate` in `rs/certification/src/lib.rs`, a local struct is defined to deserialize the delegation certificate's state tree:

```rust
#[derive(Debug, Deserialize)]
struct SubnetCertificateData {
    #[allow(unused)] // currently delegation timestamps are not checked
    time: Leb128EncodedU64,
    subnet: BTreeMap<SubnetId, SubnetView>,
    canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
}
``` [1](#0-0) 

The function then:
1. Verifies the root-subnet threshold signature on the delegation certificate. [2](#0-1) 
2. Deserializes `SubnetCertificateData` from the tree (requiring `time` to be present). [3](#0-2) 
3. Checks canister ranges and extracts the subnet public key. [4](#0-3) 
4. **Never reads `subnet_state.time`** — the field is silently dropped.

The IC interface specification requires that the delegation certificate contain a `time` leaf (the test `should_fail_to_validate_delegation_cert_if_time_missing` confirms the field must be present for deserialization to succeed), but the value is never compared against the verifier's current time. The freshness constraint is entirely absent.

`verify_delegation_certificate` is called from `verify_certificate_internal`, which is called from the public `verify_certified_data` / `verify_certificate` / `verify_certificate_for_subnet_read_state` entry points used by every IC client library and by the NNS delegation manager. [5](#0-4) [6](#0-5) 

### Impact Explanation

A malicious boundary node (an externally reachable, unprivileged actor in the IC threat model) can:

1. **Capture** a legitimately signed delegation certificate from any past IC response (these are public).
2. **Replay** that old delegation certificate in responses to clients.
3. Because `time` is never checked, `verify_delegation_certificate` accepts the stale certificate and returns the subnet public key it contains.

If the subnet's threshold key was rotated since the captured certificate was issued (e.g., after a planned DKG resharing or a security incident), the old delegation certificate carries the old public key. The boundary node can pair it with an old main certificate signed under the old key. The client's `verify_certified_data` call succeeds, returning stale certified data without any indication that the data is outdated. The client receives the certificate's `time` value from the main certificate, but the delegation certificate's own staleness is invisible.

Even without a key rotation, replaying an old delegation certificate with outdated canister-range information could cause a client to accept a certificate for a canister that has since been migrated to a different subnet, undermining the canister-range integrity guarantee.

### Likelihood Explanation

Boundary nodes are explicitly not fully trusted in the IC security model and are reachable by any unprivileged user. Old delegation certificates are publicly observable in any IC response. No private key material, governance majority, or privileged access is required. The attack requires only that a boundary node be malicious or compromised, which is a realistic threat the IC's design is supposed to defend against. The missing check is explicitly acknowledged in the source code comment, confirming it is a known gap rather than an oversight in analysis.

### Recommendation

After verifying the delegation certificate's signature, compare `subnet_state.time` against the verifier's current time and reject certificates whose timestamp is older than an acceptable staleness bound (consistent with how the main certificate's `time` is used):

```rust
let cert_time = Time::from_nanos_since_unix_epoch(subnet_state.time.0);
if cert_time + MAX_DELEGATION_CERTIFICATE_AGE < current_time {
    return Err(CertificateValidationError::StaleDelegationCertificate(...));
}
```

A `current_time` parameter (already available at all call sites via `verify_certified_data`) should be threaded into `verify_delegation_certificate`.

### Proof of Concept

1. Obtain any valid IC certificate with a delegation (e.g., from any application subnet's `/api/v2/canister/<id>/read_state` response). Extract the `delegation.certificate` bytes.
2. Call `verify_delegation_certificate(&old_delegation_bytes, &subnet_id, &root_pk, Some(&canister_id), false)`.
3. Observe that the call succeeds regardless of how old `old_delegation_bytes` is — the `time` field inside the delegation tree is parsed but the returned `subnet_state.time` value is never compared against any bound.
4. The existing test `should_fail_to_validate_delegation_cert_if_time_missing` (which asserts that a missing `time` field causes a deserialization error) confirms the field is required to be present, yet no corresponding test exists that asserts rejection of a certificate whose `time` is in the distant past — because no such rejection occurs. [7](#0-6)

### Citations

**File:** rs/certification/src/lib.rs (L323-351)
```rust
fn verify_certificate_internal(
    certificate: &[u8],
    canister_id: &CanisterId,
    root_pk: &ThresholdSigPublicKey,
    use_signature_cache: bool,
) -> Result<(Certificate, Option<DelegationSubnetInfo>), CertificateValidationError> {
    let certificate: Certificate = parse_certificate(certificate)?;
    let (key, delegation_info) = if let Some(delegation) = &certificate.delegation {
        let subnet_id = PrincipalId::try_from(&*delegation.subnet_id)
            .map(SubnetId::from)
            .map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to parse delegation subnet id: {err}"
                ))
            })?;
        let (key, info) = verify_delegation_certificate(
            &delegation.certificate,
            &subnet_id,
            root_pk,
            Some(canister_id),
            use_signature_cache,
        )?;
        (key, Some(info))
    } else {
        (*root_pk, None)
    };

    verify_certificate_signature(&certificate, &key, use_signature_cache)?;
    Ok((certificate, delegation_info))
```

**File:** rs/certification/src/lib.rs (L373-379)
```rust
    #[derive(Debug, Deserialize)]
    struct SubnetCertificateData {
        #[allow(unused)] // currently delegation timestamps are not checked
        time: Leb128EncodedU64,
        subnet: BTreeMap<SubnetId, SubnetView>,
        canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
    }
```

**File:** rs/certification/src/lib.rs (L389-389)
```rust
    verify_certificate_signature(&certificate, root_pk, use_signature_cache)?;
```

**File:** rs/certification/src/lib.rs (L391-398)
```rust
    let replica_labeled_tree = parse_tree(certificate.tree)?;
    let subnet_state =
        SubnetCertificateData::deserialize(LabeledTreeDeserializer::new(&replica_labeled_tree))
            .map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to unpack replica state from a labeled tree: {err}"
                ))
            })?;
```

**File:** rs/certification/src/lib.rs (L409-471)
```rust
    if let Some(canister_id) = canister_id {
        if subnet_info.canister_ranges.is_none() && subnet_state.canister_ranges.is_none() {
            return Err(CertificateValidationError::MalformedHashTree(String::from(
                "state tree doesn't have canister ranges",
            )));
        }

        let canister_id_ranges_contain = |canister_id, canister_id_ranges: Vec<_>| {
            canister_id_ranges
                .iter()
                .any(|(range_start, range_end)| (range_start..=range_end).contains(&canister_id))
        };

        // Check `/subnet/<subnet_id>/canister_ranges`
        if let Some(canister_ranges) = &subnet_info.canister_ranges {
            let canister_id_ranges = serde_cbor::from_slice(canister_ranges).map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to unpack canister range: {err}"
                ))
            })?;

            if !canister_id_ranges_contain(canister_id, canister_id_ranges) {
                return Err(CertificateValidationError::CanisterIdOutOfRange);
            }
        }

        // Check `/canister_ranges/<subnet_id>`
        if let Some(canister_ranges_per_subnet_id) = &subnet_state.canister_ranges {
            let canister_ranges =
                canister_ranges_per_subnet_id
                    .get(subnet_id)
                    .ok_or_else(|| {
                        CertificateValidationError::MalformedHashTree(format!(
                            "cannot find canister ranges for subnet {subnet_id} in the tree"
                        ))
                    })?;

            // Find the leaf which *might* cover the canister ID.
            let Some((_canister_id, canister_ranges)) = canister_ranges
                .range((
                    std::ops::Bound::Unbounded,
                    std::ops::Bound::Included(canister_id),
                ))
                .last()
            else {
                return Err(CertificateValidationError::CanisterIdOutOfRange);
            };

            let canister_id_ranges = serde_cbor::from_slice(canister_ranges).map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to unpack canister range: {err}",
                ))
            })?;

            if !canister_id_ranges_contain(canister_id, canister_id_ranges) {
                return Err(CertificateValidationError::CanisterIdOutOfRange);
            }
        }
    }

    let public_key = parse_threshold_sig_key_from_der(&subnet_info.public_key).map_err(|err| {
        CertificateValidationError::DeserError(format!("failed to deserialize public key: {err}"))
    })?;
```

**File:** rs/http_endpoints/nns_delegation_manager/src/nns_delegation_reader.rs (L1-6)
```rust
use ic_crypto_tree_hash::{
    FilterBuilder, LabeledTree, LookupLowerBoundStatus, Path, lookup_lower_bound,
    sparse_labeled_tree_from_paths,
};
use ic_logger::{ReplicaLogger, warn};
use ic_types::{
```

**File:** rs/certification/src/tests.rs (L811-839)
```rust
#[test]
fn should_fail_to_validate_delegation_cert_if_time_missing() {
    let rng = &mut reproducible_rng();
    let subnet_id = subnet_id(42);

    let (cert, root_pk, _cbor) = CertificateBuilder::new_with_rng(CanisterData {
                    canister_id: canister_id(1),
                    certified_data: random_certified_data(),
                },rng)
                .with_delegation(CertificateBuilder::new_with_rng(CustomTree(LabeledTree::SubTree(
                    flatmap![
                        Label::from("subnet") => LabeledTree::SubTree(flatmap![
                            Label::from(subnet_id.get_ref().to_vec()) => LabeledTree::SubTree(flatmap![
                                Label::from("canister_ranges") => LabeledTree::Leaf(b"dummy_canister_ranges".to_vec()),
                                Label::from("public_key") => LabeledTree::Leaf(b"dummy_public_key".to_vec()),
                            ])
                        ]),
                        // time is missing here
                    ],
                )),rng))
                .with_delegation_subnet_id(subnet_id)
                .build();
    let delegation = cert.delegation.expect("missing delegation");

    assert_matches!(
        validate_subnet_delegation_certificate_with_and_without_cache(&delegation.certificate, &subnet_id, &root_pk),
        Err(CertificateValidationError::DeserError(e))
        if e.contains("failed to unpack replica state from a labeled tree")
    );
```
