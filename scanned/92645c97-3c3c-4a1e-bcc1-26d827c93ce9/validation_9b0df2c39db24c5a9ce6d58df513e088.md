## Analysis

Let me trace the full attack path and check every guard.

**Call chain from P2P to deserialization:**

A Byzantine subnet peer sends a crafted `EcdsaSigShare` artifact over QUIC. The artifact is admitted to the unvalidated pool, then `validate_signature_share` in the signer calls `crypto_verify_sig_share`, which calls `ThresholdEcdsaSigVerifier::verify_sig_share`, which calls `ThresholdEcdsaSigShareInternal::deserialize(&share.sig_share_raw)` — with no size or type gate before the call. [1](#0-0) 

The deserializer is a bare `serde_cbor::from_slice`: [2](#0-1) 

**What serde_cbor does with crafted input:**

`ThresholdEcdsaSigShareInternal` contains two `CommitmentOpening` fields, each holding `EccScalar` values. `EccScalar` uses a custom serde impl that reads an `EccScalarSerializationHelper(#[serde(with = "serde_bytes")] Vec<u8>)`: [3](#0-2) 

The `serde_bytes` attribute causes serde_cbor to deserialize the field as a CBOR definite-length byte string. In serde_cbor 0.11.x, `read_to_vec(len)` calls `Vec::with_capacity(len)` with the raw CBOR-declared length **before** reading any actual bytes. A CBOR byte string header claiming length ≥ `isize::MAX` (e.g., 8-byte length field `0x8000000000000000`) causes `Vec::with_capacity` to panic with "capacity overflow" — a hard Rust panic, not a recoverable error.

**No pre-deserialization size gate exists:**

The `sig_share_raw` field is a plain `Vec<u8>` with no length validation: [4](#0-3) 

The P2P artifact downloader explicitly disables body size limits: [5](#0-4) 

**The `EccScalar` length check happens too late:**

`EccScalar::deserialize_tagged` checks the byte length only *after* serde_cbor has already attempted the allocation: [6](#0-5) 

**Recursion depth is not the issue here** — the structure is flat (two fields, each a simple enum). The attack vector is the byte-string length prefix, not nesting depth.

**Attacker capability:** A single Byzantine subnet member (authenticated via TLS, below the fault threshold) can send a crafted `EcdsaSigShare` artifact. The signer processes all unvalidated shares: [7](#0-6) 

**However — critical mitigating factor:**

The `QUIC` transport enforces `MAX_MESSAGE_SIZE_BYTES` at the transport layer. The crafted CBOR message itself is tiny (the malicious length prefix is only 9 bytes in the CBOR header), so the transport limit does **not** prevent delivery. The panic occurs when serde_cbor reads the header and calls `Vec::with_capacity(usize::MAX)` before attempting to read the claimed bytes.

**Severity qualification:**

- Key-share disclosure via process restart: **not realistic** — HSM/vault state is not exposed on restart.
- Consensus stall: **realistic** — a single Byzantine node can repeatedly crash the signer thread on all honest replicas, preventing signature share validation. Replicas restart but the attack is repeatable.
- The `panic_on_join_err` pattern in the consensus manager re-panics on task panics, propagating the crash upward. [8](#0-7) 

---

### Title
Unbounded allocation panic in `ThresholdEcdsaSigShareInternal::deserialize` via crafted CBOR byte-string length prefix — (`rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs`)

### Summary
`ThresholdEcdsaSigShareInternal::deserialize` passes untrusted peer-supplied bytes directly to `serde_cbor::from_slice` with no prior length or structure gate. A Byzantine subnet peer can craft a CBOR payload where the `EccScalar` byte-string field declares a length ≥ `isize::MAX`. serde_cbor calls `Vec::with_capacity(usize::MAX)` before reading any data, triggering a Rust panic that crashes the signer thread and disrupts consensus participation.

### Finding Description
The deserialization path is:

```
P2P artifact → unvalidated pool → validate_signature_share
  → crypto_verify_sig_share → verify_sig_share (ecdsa.rs:115)
  → ThresholdEcdsaSigShareInternal::deserialize (ecdsa.rs:224-227)
  → serde_cbor::from_slice (no size gate)
  → EccScalar custom Deserialize → EccScalarSerializationHelper (serde_bytes Vec<u8>)
  → serde_cbor read_to_vec(len) → Vec::with_capacity(usize::MAX) → PANIC
```

The `sig_share_raw` field is a raw `Vec<u8>` with no length bound. The P2P layer disables body size limits. The CBOR length prefix attack uses a 9-byte message (1-byte major type + 8-byte length field) to trigger the panic before any actual data is read.

### Impact Explanation
A single Byzantine subnet member can repeatedly crash the signer validation thread on honest replicas, preventing them from producing or validating ECDSA signature shares. This stalls threshold signing for any active signing requests on the subnet. Replicas restart but the attack is immediately repeatable.

### Likelihood Explanation
Any authenticated subnet member (below the fault threshold) can execute this with a trivially crafted 9-byte CBOR payload. No cryptographic material or privileged access is required beyond subnet membership.

### Recommendation
Add a maximum byte length check on `sig_share_raw` before calling `deserialize`. A legitimate sig share for any supported curve is bounded (two `EccScalar` values = ~100 bytes of CBOR). Reject any `sig_share_raw` exceeding, e.g., 512 bytes before deserialization. Additionally, consider replacing `serde_cbor::from_slice` with a length-limited wrapper or migrating to a format with explicit length bounds (e.g., the fixed-length encoding already used by `ThresholdEcdsaCombinedSigInternal::deserialize`).

### Proof of Concept
```rust
// Craft: CBOR map {"sigma_numerator": [h'<9-byte-header-claiming-2^63-bytes>]}
// 0xa1                          -- map(1)
//   0x6f "sigma_numerator"      -- text(15)
//   0x81                        -- array(1)  [EccScalarSerializationHelper tuple]
//     0x5b 0x80 0x00 ... 0x00   -- bytes(2^63) -- triggers Vec::with_capacity(2^63) → panic
let crafted: Vec<u8> = vec![
    0xa1, 0x6f, b's',b'i',b'g',b'm',b'a',b'_',b'n',b'u',b'm',b'e',b'r',b'a',b't',b'o',b'r',
    0x81,
    0x5b, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
];
// This panics:
let _ = ThresholdEcdsaSigShareInternal::deserialize(&crafted);
```

### Citations

**File:** rs/crypto/src/sign/canister_threshold_sig/ecdsa.rs (L114-119)
```rust
    let sig_share =
        ThresholdEcdsaSigShareInternal::deserialize(&share.sig_share_raw).map_err(|e| {
            ThresholdEcdsaVerifySigShareError::SerializationError {
                internal_error: format!("failed to deserialize signature share: {}", e.0),
            }
        })?;
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs (L224-227)
```rust
    pub fn deserialize(raw: &[u8]) -> CanisterThresholdSerializationResult<Self> {
        serde_cbor::from_slice::<Self>(raw)
            .map_err(|e| CanisterThresholdSerializationError(format!("{e}")))
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group.rs (L344-358)
```rust
    /// Deserialize a scalar value (with tag)
    pub fn deserialize_tagged(bytes: &[u8]) -> CanisterThresholdSerializationResult<Self> {
        if bytes.is_empty() {
            return Err(CanisterThresholdSerializationError(
                "failed to deserialize tagged EccScalar: empty bytestring".to_string(),
            ));
        }

        match EccCurveType::from_tag(bytes[0]) {
            Some(curve) => Self::deserialize(curve, &bytes[1..]),
            None => Err(CanisterThresholdSerializationError(
                "failed to deserialize tagged EccScalar: unknown curve tag".to_string(),
            )),
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group.rs (L507-523)
```rust
#[derive(Deserialize, Serialize)]
struct EccScalarSerializationHelper(#[serde(with = "serde_bytes")] Vec<u8>);

impl Serialize for EccScalar {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let helper = EccScalarSerializationHelper(self.serialize_tagged());
        helper.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for EccScalar {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let helper: EccScalarSerializationHelper = Deserialize::deserialize(deserializer)?;
        EccScalar::deserialize_tagged(&helper.0)
            .map_err(|e| serde::de::Error::custom(format!("{e:?}")))
    }
}
```

**File:** rs/types/types/src/crypto/canister_threshold_sig.rs (L478-482)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Deserialize, Serialize)]
pub struct ThresholdEcdsaSigShare {
    #[serde(with = "serde_bytes")]
    pub sig_share_raw: Vec<u8>,
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L44-53)
```rust
fn build_axum_router<Artifact: PbArtifact>(pool: ValidatedPoolReaderRef<Artifact>) -> Router {
    Router::new()
        .route(
            &format!("/{}/rpc", uri_prefix::<Artifact>()),
            any(rpc_handler),
        )
        .with_state(pool)
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable())
}
```

**File:** rs/consensus/idkg/src/signer.rs (L249-281)
```rust
    fn validate_signature_share(
        &self,
        idkg_pool: &dyn IDkgPool,
        id: IDkgMessageId,
        share: SigShare,
        inputs: &ThresholdSigInputs,
    ) -> Option<IDkgChangeAction> {
        {
            let valid_sig_share_signers = self.validated_sig_share_signers.read().unwrap();
            let maybe_signers = valid_sig_share_signers.get(&share.request_id());
            if maybe_signers.is_some_and(|signers| signers.contains(&share.signer())) {
                self.metrics
                    .sign_errors_inc("duplicate_sig_share_cache_hit");
                return Some(IDkgChangeAction::RemoveUnvalidated(id));
            }

            if Self::inputs_already_have_enough_shares(inputs, maybe_signers) {
                // We already have enough valid shares for this request
                return Some(IDkgChangeAction::RemoveUnvalidated(id));
            }
        }

        let signer = share.signer();
        let request_id = share.request_id();
        let scheme = share.scheme();
        if Self::signer_has_issued_share(idkg_pool, &signer, &request_id, scheme) {
            // The node already sent a valid share for this request
            self.metrics.sign_errors_inc("duplicate_sig_share");
            return Some(IDkgChangeAction::RemoveUnvalidated(id));
        }

        let share_string = share.to_string();
        match self.crypto_verify_sig_share(inputs, share, idkg_pool.stats()) {
```

**File:** rs/p2p/consensus_manager/src/sender.rs (L43-53)
```rust
fn panic_on_join_err<T>(result: Result<T, JoinError>) -> T {
    match result {
        Ok(value) => value,
        Err(err) => {
            if err.is_panic() {
                panic::resume_unwind(err.into_panic());
            } else {
                panic!("Join error: {err:?}");
            }
        }
    }
```
