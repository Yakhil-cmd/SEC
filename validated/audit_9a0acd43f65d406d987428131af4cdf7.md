### Title
Delegation Certificate Timestamp Not Validated in `verify_delegation_certificate` — Stale Delegation Accepted — (`rs/certification/src/lib.rs`)

---

### Summary

The `verify_delegation_certificate` function in `rs/certification/src/lib.rs` parses the `time` field from the NNS-signed subnet delegation certificate but explicitly never validates it against the current time. An attacker who possesses a stale delegation certificate (e.g., one issued before a subnet key rotation) can replay it to authenticate canister signatures or certified data that would otherwise be rejected under the current subnet key.

---

### Finding Description

Inside `verify_delegation_certificate`, the deserialized `SubnetCertificateData` struct contains a `time` field that is explicitly annotated `#[allow(unused)] // currently delegation timestamps are not checked`:

```rust
struct SubnetCertificateData {
    #[allow(unused)] // currently delegation timestamps are not checked
    time: Leb128EncodedU64,
    subnet: BTreeMap<SubnetId, SubnetView>,
    canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
}
``` [1](#0-0) 

The function verifies the cryptographic signature of the delegation certificate against the NNS root public key and checks canister ranges, but it never compares the embedded `time` value to the current wall-clock time or any freshness bound. [2](#0-1) 

This function is called from `verify_certificate_internal` (used for all canister-state certificate verification) and from `verify_certificate_for_subnet_read_state`: [3](#0-2) 

The most security-sensitive call site is `verify_certified_data_with_cache_for_canister_sig`, which is used to verify IC canister signatures (ICCSA) and explicitly rejects cloud-engine subnets — but still does not check the delegation timestamp: [4](#0-3) 

The `verify_certified_data` path is also used by the registry data provider and the ICP ledger block synchronizer: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

A delegation certificate is issued by the NNS root subnet and binds a subnet's BLS public key to its canister ranges at a specific point in time. If a subnet's threshold key is rotated (or the subnet is decommissioned), the NNS issues a new delegation certificate with the new key. Because the old delegation certificate is cryptographically valid (it was correctly signed by the NNS root key) and its timestamp is never checked, an attacker who:

1. Retains an old delegation certificate (these are public — returned in every `/api/v2/canister/.../read_state` response that crosses a subnet boundary), and
2. Possesses the old subnet signing key (e.g., from a compromised subnet node set that has since been rotated),

can forge canister signatures (`IcCanisterSignatureAlgPublicKeyDer`) that pass `verify_certified_data_with_cache_for_canister_sig`. This allows the attacker to authenticate ingress messages as any principal whose identity is derived from a canister signature, bypassing the ingress authentication pipeline.

---

### Likelihood Explanation

Subnet key rotations are rare on the IC today, but the vulnerability is explicitly acknowledged in production code with the comment `// currently delegation timestamps are not checked`. The delegation certificates are publicly observable by any boundary-node user or API caller. The attack requires possession of an old subnet signing key, which raises the bar — but the absence of any timestamp check means there is no defense-in-depth once a key is compromised and later rotated.

---

### Recommendation

- **Short term**: In `verify_delegation_certificate`, compare the parsed `time` field against a caller-supplied `current_time` parameter (or a configurable maximum staleness window, e.g., 24 hours). Reject delegation certificates whose embedded time is older than the allowed window.
- **Long term**: Propagate a `current_time` argument through `verify_certified_data`, `verify_certificate`, and related public APIs so that all callers can enforce freshness. Align this with the IC specification's intent that delegation certificates reflect a recent NNS state.

---

### Proof of Concept

1. Observe a `/api/v2/canister/<cid>/read_state` response from an application subnet. Extract the `delegation.certificate` CBOR field — this is the NNS-signed delegation certificate containing the subnet's BLS public key and the `time` at which it was issued.
2. Retain this certificate indefinitely.
3. After the subnet undergoes a key rotation (new NNS delegation issued), replay the old delegation certificate in a crafted canister-signature verification call to `verify_certified_data_with_cache_for_canister_sig`.
4. Because `verify_delegation_certificate` never checks the `time` field against the current time, the old delegation certificate is accepted, and the old subnet key is treated as authoritative — allowing forgery of canister signatures under the rotated-away key. [7](#0-6)

### Citations

**File:** rs/certification/src/lib.rs (L156-172)
```rust
pub fn verify_certified_data_with_cache_for_canister_sig(
    certificate: &[u8],
    canister_id: &CanisterId,
    root_pk: &ThresholdSigPublicKey,
    certified_data: &[u8],
) -> Result<Time, CertificateValidationError> {
    let (time, delegation_info) =
        verify_certified_data_internal(certificate, canister_id, root_pk, certified_data, true)?;
    if let Some(info) = delegation_info
        && matches!(info.subnet_type.as_deref(), None | Some("cloud_engine"))
    {
        return Err(CertificateValidationError::UntrustedDelegationSubnet(
            info.subnet_id,
        ));
    }
    Ok(time)
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

**File:** rs/certification/src/lib.rs (L357-400)
```rust
pub fn verify_delegation_certificate(
    certificate: &[u8],
    subnet_id: &SubnetId,
    root_pk: &ThresholdSigPublicKey,
    canister_id: Option<&CanisterId>,
    use_signature_cache: bool,
) -> Result<(ThresholdSigPublicKey, DelegationSubnetInfo), CertificateValidationError> {
    #[derive(Debug, Deserialize)]
    struct SubnetView {
        canister_ranges: Option<Blob>,
        public_key: Blob,
        r#type: Option<String>, // added in V25 certifications
    }

    type TreeCanisterRanges = BTreeMap<CanisterId, Blob>;

    #[derive(Debug, Deserialize)]
    struct SubnetCertificateData {
        #[allow(unused)] // currently delegation timestamps are not checked
        time: Leb128EncodedU64,
        subnet: BTreeMap<SubnetId, SubnetView>,
        canister_ranges: Option<BTreeMap<SubnetId, TreeCanisterRanges>>,
    }

    let certificate: Certificate = parse_certificate(certificate)?;

    if certificate.delegation.is_some() {
        // the specification would allow this, but since the current IC will never do that all certificates
        // with nested delegations are automatically invalid. We abort here to avoid unnecessary computation.
        return Err(CertificateValidationError::MultipleSubnetDelegationsNotAllowed);
    };

    verify_certificate_signature(&certificate, root_pk, use_signature_cache)?;

    let replica_labeled_tree = parse_tree(certificate.tree)?;
    let subnet_state =
        SubnetCertificateData::deserialize(LabeledTreeDeserializer::new(&replica_labeled_tree))
            .map_err(|err| {
                CertificateValidationError::DeserError(format!(
                    "failed to unpack replica state from a labeled tree: {err}"
                ))
            })?;

    let subnet_info = subnet_state.subnet.get(subnet_id).ok_or_else(|| {
```

**File:** rs/registry/nns_data_provider/src/certification.rs (L203-209)
```rust
    let time = verify_certified_data(
        &certified_response.certificate[..],
        canister_id,
        nns_pk,
        mixed_hash_tree.digest().as_bytes(),
    )
    .map_err(embed_certificate_error)?;
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/certification.rs (L11-24)
```rust
pub(crate) fn verify_block_hash(
    cert: &icp_ledger::Certification,
    hash: HashOf<EncodedBlock>,
    info: &VerificationInfo,
) -> Result<(), String> {
    verify_certified_data(
        cert.as_ref()
            .ok_or("verify tip failed: no data certificate present")?,
        &info.canister_id,
        &info.root_key,
        &hash.into_bytes(),
    )
    .map(|_| ()) // we don't need the result so we discard it
    .map_err(|e| format!("Certification error: {e:?}"))
```
