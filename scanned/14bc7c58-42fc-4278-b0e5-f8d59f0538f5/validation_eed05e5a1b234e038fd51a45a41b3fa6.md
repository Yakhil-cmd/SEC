### Title
Unchecked Delegation Certificate Timestamp Allows Stale Subnet Key Acceptance - (File: `rs/certification/src/lib.rs`)

### Summary
The `verify_delegation_certificate` function in `rs/certification/src/lib.rs` parses the `time` field from a subnet delegation certificate's state tree but explicitly does **not** check it against the current time. The code comment reads `#[allow(unused)] // currently delegation timestamps are not checked`. This means a delegation certificate carrying an arbitrarily old subnet public key is accepted as valid, directly analogous to the reported "expired oracle key" issue.

### Finding Description
In `rs/certification/src/lib.rs`, the `verify_delegation_certificate` function deserializes the delegation certificate's state tree into a `SubnetCertificateData` struct:

```rust
struct SubnetCertificateData {
    #[allow(unused)] // currently delegation timestamps are not checked
    time: Leb128EncodedU64,
    subnet: BTreeMap<SubnetId, SubnetView>,
    canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
}
```

The `time` field is parsed (its presence is required for deserialization to succeed) but is never compared against any current-time value. No staleness bound is enforced. The function returns the subnet's public key extracted from the certificate without any check that the certificate's timestamp is recent. [1](#0-0) 

This function is called from multiple paths:
- `verify_certificate_internal` (used by `verify_certificate`, `verify_certified_data`) — the primary path for verifying canister signatures and read-state responses
- `verify_certificate_for_subnet_read_state` — for subnet read-state endpoints
- `validate_subnet_delegation_certificate` / `validate_subnet_delegation_certificate_with_cache` — used by the NNS delegation manager [2](#0-1) [3](#0-2) [4](#0-3) 

The `packages/ic-signature-verification/src/canister_sig.rs` `verify_delegation` function similarly extracts the subnet public key from the delegation certificate without any timestamp check: [5](#0-4) 

### Impact Explanation
An attacker who can serve a boundary node or intercept a read-state response can replay a **cryptographically valid but arbitrarily old** delegation certificate. The old certificate carries the subnet's public key from a past epoch. If the subnet has since rotated its threshold key (e.g., after a key compromise or scheduled rotation), the verifier will still accept signatures produced under the old key as valid. This breaks the security guarantee of key rotation: a compromised old subnet key remains permanently usable for forging certified state responses accepted by any verifier using `verify_certificate` or `verify_canister_sig`.

For the NNS delegation path specifically, a boundary node serving a stale NNS delegation certificate would cause the replica to accept responses signed by an old (potentially compromised) subnet key as authoritative.

### Likelihood Explanation
The IC does rotate subnet threshold keys via NiDKG. The `validate_subnet_delegation_certificate` call in the NNS delegation manager is reachable by any node that fetches a delegation from a peer. A malicious boundary node or a network-level attacker below the consensus fault threshold can serve a stale delegation certificate. The `verify_canister_sig` path in `packages/ic-signature-verification` is callable by any external verifier (e.g., a dapp or wallet) that uses the IC's canister signature verification library. The likelihood is moderate: it requires a past key rotation event and the ability to serve stale certificates, but no privileged access.

### Recommendation
Pass the current time into `verify_delegation_certificate` and enforce a maximum staleness bound on `subnet_state.time`. Reject delegation certificates whose embedded `time` is older than a configurable threshold (e.g., the NiDKG epoch duration or a fixed bound such as 24 hours). The `time` field is already parsed and available; only the comparison is missing. [6](#0-5) 

### Proof of Concept
1. Obtain a valid delegation certificate `D_old` for subnet `S` at time `T_old`, signed by the NNS root key, containing subnet public key `PK_old`.
2. The subnet rotates its threshold key at time `T_new > T_old`; `PK_old` is no longer the active key.
3. Forge a certified state response signed under `PK_old`.
4. Attach `D_old` as the delegation in the certificate.
5. Call `verify_certificate` (or `verify_canister_sig`) with the forged certificate.
6. `verify_delegation_certificate` parses `D_old`, finds `time = T_old`, ignores it (the `#[allow(unused)]` field), extracts `PK_old`, and verifies the BLS signature over the forged state tree using `PK_old` — which succeeds.
7. The forged certified state is accepted as valid. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/certification/src/lib.rs (L283-318)
```rust
pub fn verify_certificate_for_subnet_read_state(
    certificate: &[u8],
    subnet_id: &SubnetId,
    root_pk: &ThresholdSigPublicKey,
) -> Result<Certificate, CertificateValidationError> {
    let certificate: Certificate = parse_certificate(certificate)?;
    let key = if let Some(delegation) = &certificate.delegation {
        let delegation_subnet_id = PrincipalId::try_from(&*delegation.subnet_id)
            .map(SubnetId::from)
            .map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to parse delegation subnet id: {err}"
                ))
            })?;
        if subnet_id != &delegation_subnet_id {
            return Err(CertificateValidationError::SubnetIdMismatch {
                provided_subnet_id: *subnet_id,
                delegation_subnet_id,
            });
        }

        let (key, _delegation_info) = verify_delegation_certificate(
            &delegation.certificate,
            subnet_id,
            root_pk,
            None,
            false,
        )?;
        key
    } else {
        *root_pk
    };

    verify_certificate_signature(&certificate, &key, false)?;
    Ok(certificate)
}
```

**File:** rs/certification/src/lib.rs (L323-352)
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
}
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

**File:** rs/certification/src/lib.rs (L389-398)
```rust
    verify_certificate_signature(&certificate, root_pk, use_signature_cache)?;

    let replica_labeled_tree = parse_tree(certificate.tree)?;
    let subnet_state =
        SubnetCertificateData::deserialize(LabeledTreeDeserializer::new(&replica_labeled_tree))
            .map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to unpack replica state from a labeled tree: {err}"
                ))
            })?;
```

**File:** rs/certification/src/lib.rs (L469-479)
```rust
    let public_key = parse_threshold_sig_key_from_der(&subnet_info.public_key).map_err(|err| {
        CertificateValidationError::DeserError(format!("failed to deserialize public key: {err}"))
    })?;

    Ok((
        public_key,
        DelegationSubnetInfo {
            subnet_id: *subnet_id,
            subnet_type: subnet_info.r#type.clone(),
        },
    ))
```

**File:** rs/http_endpoints/nns_delegation_manager/src/nns_delegation_manager.rs (L403-413)
```rust
    let root_threshold_public_key =
        get_root_threshold_public_key(registry_client, registry_version, nns_subnet_id).map_err(
            |err| format!("could not retrieve threshold root public key from registry: {err}"),
        )?;

    validate_subnet_delegation_certificate(
        &response.certificate,
        &subnet_id,
        &root_threshold_public_key,
    )
    .map_err(|err| format!("invalid subnet delegation certificate: {err:?} "))?;
```

**File:** packages/ic-signature-verification/src/canister_sig.rs (L113-176)
```rust
fn verify_delegation(
    delegation: &Delegation,
    signing_canister_id: Principal,
    root_public_key: &[u8],
) -> Result<Vec<u8>, String> {
    let cert: Certificate = parse_certificate_cbor(&delegation.certificate)
        .map_err(|e| format!("invalid delegation certificate: {e}"))?;

    // disallow nested delegations
    if cert.delegation.is_some() {
        return Err("multiple delegations not allowed".to_string());
    }

    check_bls_signature(&cert, root_public_key)?;

    // Try new structure first: /canister_ranges/<subnet_id>/<range_key>
    let canister_ranges_path = ["canister_ranges".as_bytes(), delegation.subnet_id.as_ref()];
    let canister_in_ranges = match cert.tree.lookup_subtree(&canister_ranges_path) {
        SubtreeLookupResult::Found(subnet_tree) => {
            match find_leaf_for_principal(subnet_tree.as_ref(), signing_canister_id.as_slice()) {
                Some(range_data) => {
                    let subnet_ranges: Vec<(Principal, Principal)> =
                        serde_cbor::from_slice(range_data)
                            .map_err(|e| format!("invalid canister range: {e}"))?;
                    principal_is_within_ranges(&signing_canister_id, &subnet_ranges)
                }
                None => false,
            }
        }
        SubtreeLookupResult::Absent | SubtreeLookupResult::Unknown => {
            let old_canister_ranges_path = [
                "subnet".as_bytes(),
                delegation.subnet_id.as_ref(),
                "canister_ranges".as_bytes(),
            ];
            match cert.tree.lookup_path(&old_canister_ranges_path) {
                LookupResult::Found(old_range_data) => {
                    let canister_ranges: Vec<(Principal, Principal)> =
                        serde_cbor::from_slice(old_range_data)
                            .map_err(|e| format!("invalid canister range: {e}"))?;
                    principal_is_within_ranges(&signing_canister_id, &canister_ranges)
                }
                _ => {
                    return Err("canister_ranges-entry not found".to_string());
                }
            }
        }
    };

    if !canister_in_ranges {
        return Err("signing canister id not in canister_ranges".to_string());
    }

    // lookup the public key delegated to
    let public_key_path = [
        "subnet".as_bytes(),
        delegation.subnet_id.as_ref(),
        "public_key".as_bytes(),
    ];
    let LookupResult::Found(subnet_public_key_der) = cert.tree.lookup_path(&public_key_path) else {
        return Err("subnet public key not found".to_string());
    };
    extract_raw_root_pk_from_der(subnet_public_key_der)
}
```
