### Title
`SenderInfoContent` Signature Lacks Expiry and Target-Canister Binding, Enabling Cross-Canister and Temporal Replay — (`rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `SenderInfoContent` signable type used to verify the `sender_info` field of IC HTTP requests signs only the raw `info` bytes under the domain separator `"ic-sender-info"`. It binds neither the target canister ID nor any expiry/deadline. Because IC canister-signature certificates carry no protocol-level expiry, a valid `sender_info` canister signature can be replayed verbatim to any canister and at any future time, as long as the signing canister's certified data still contains the corresponding hash — or as long as the certificate snapshot embedded in the signature was ever valid.

---

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over a raw byte slice:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // only the raw info bytes
    }
}
``` [1](#0-0) 

Its `SignatureDomain` implementation prepends only the fixed string `"ic-sender-info"`:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
``` [2](#0-1) 

The resulting signed bytes are therefore: `\x0Eic-sender-info || info_bytes` — with **no target canister ID, no sender principal, and no expiry timestamp**.

The replica-side verifier in `verify_sender_info_canister_sig` constructs exactly this content and verifies the canister signature against it:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

verify_canister_sig_with_fallback!(
    validator,
    &canister_sig,
    &sender_info_content,
    &public_key,
    root_of_trust_provider,
    ...
);
``` [3](#0-2) 

No check is performed on any expiry embedded in the `info` bytes, nor is there any protocol-level enforcement that the signature was produced for the specific target canister or sender. The only binding enforced is that the canister ID encoded in `sender_pubkey` matches the declared `signer` field. [4](#0-3) 

IC canister signatures embed a certificate — a BLS-signed snapshot of the canister's certified data — that carries **no expiry field**. Once a certificate is issued by the subnet, it remains cryptographically valid indefinitely. The `verify_canister_sig` path does not reject certificates based on age: [5](#0-4) 

---

### Impact Explanation

Any canister that calls `msg_caller_info_data()` and uses the returned bytes for access-control decisions (e.g., "caller has KYC status", "caller is over 18", "caller holds a specific credential") is vulnerable to:

1. **Temporal replay**: A user obtains a `sender_info` attestation from Internet Identity (or any signing canister). Even after the signing canister revokes the attribute (by updating its certified data), the user retains the old certificate snapshot embedded in the canister signature. Because the certificate carries no expiry, the old `sender_info` remains verifiable by the replica and can be submitted in new requests indefinitely.

2. **Cross-canister replay**: The same `{info, signer, sig}` triple can be included verbatim in requests to any canister on any subnet. There is no canister-ID field in the signed message to prevent this.

The impact is loss of access-control integrity for any canister that relies on `sender_info` for authorization — equivalent to the RabbitHole finding where unintended minting occurred because the signature lacked chain-ID and contract-address binding.

---

### Likelihood Explanation

The `sender_info` feature is present in production replica code and is actively tested end-to-end. Internet Identity is the primary intended signer. As dapps adopt `sender_info` for attribute-based access control (the stated purpose of the feature), the attack surface grows. The attacker is the legitimate user themselves (or anyone who captures the HTTP envelope), requiring no privileged access — only possession of a previously valid `sender_info` blob.

---

### Recommendation

Bind the signed content to the context in which it is valid:

1. **Include the target canister ID** in `SenderInfoContent` so a signature issued for canister A cannot be replayed to canister B.
2. **Include an expiry timestamp** (or a nonce tied to the outer request's `ingress_expiry`) in `SenderInfoContent` so stale attestations are rejected at the protocol level.
3. Alternatively, adopt a structure analogous to IC delegations (`Delegation` already includes `expiration` and `targets`) so the same replay-protection machinery applies.

The minimal change is to extend `SenderInfoContent` to carry `(info_bytes, target_canister_id, expiry_ns)` and update `verify_sender_info_canister_sig` to enforce the expiry and canister-ID match.

---

### Proof of Concept

1. User A obtains a `sender_info` from Internet Identity: `{info: b"kyc=true", signer: II_canister_id, sig: <canister_sig>}`.
2. User A submits a request to canister C1 with this `sender_info`. The replica verifies the canister signature over `\x0Eic-sender-info || b"kyc=true"` and accepts it. C1 grants access.
3. Internet Identity revokes user A's KYC status (updates its certified data, removing the hash).
4. User A constructs a **new** HTTP request to canister C2 (or C1 again), embedding the **same** `sender_info` triple. The embedded certificate is a snapshot from step 1 — it is still a valid BLS-signed certificate from the subnet, and the replica has no mechanism to reject it as stale.
5. The replica calls `verify_sender_info_canister_sig`, which reconstructs `SenderInfoContent(b"kyc=true")`, verifies the canister signature against the old certificate, and succeeds. C2 grants access based on a revoked credential.

The attack requires only that the user retain the raw bytes of a previously accepted `sender_info` — no cryptographic capability beyond what a normal user already possesses.

### Citations

**File:** rs/types/types/src/messages/http.rs (L341-348)
```rust
#[derive(Clone, Debug)]
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
}
```

**File:** rs/types/types/src/crypto/sign.rs (L161-164)
```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
```

**File:** rs/validator/src/ingress_validation.rs (L494-521)
```rust
fn verify_sender_info_canister_sig<R: RootOfTrustProvider>(
    sender_info: &SignedSenderInfo,
    sender_pubkey_bytes: &[u8],
    validator: &dyn IngressSigVerifier,
    root_of_trust_provider: &R,
) -> Result<(), RequestValidationError>
where
    R::Error: std::error::Error,
{
    // Parse the envelope-level sender_pubkey DER to extract the raw
    // public key bytes and verify it's a valid canister signature public key.
    let pk_bytes = public_key_bytes_from_der(sender_pubkey_bytes).map_err(|e| {
        InvalidSenderInfo(format!(
            "sender_pubkey is not a valid canister signature public key: {e}"
        ))
    })?;

    // Extract the canister ID from the parsed public key and verify
    // it matches the declared signer.
    let parsed_pk = ic_crypto_iccsa::types::PublicKey::try_from(&pk_bytes)
        .map_err(|e| InvalidSenderInfo(format!("invalid canister sig public key: {e:?}")))?;
    let pubkey_canister_id = parsed_pk.signing_canister_id();
    if pubkey_canister_id != sender_info.signer {
        return Err(InvalidSenderInfo(format!(
            "signer {} does not match canister ID {} in sender_pubkey",
            sender_info.signer, pubkey_canister_id
        )));
    }
```

**File:** rs/validator/src/ingress_validation.rs (L529-543)
```rust
    // Construct the signable content (domain = "ic-sender-info")
    let sender_info_content = SenderInfoContent(&sender_info.info);
    let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

    verify_canister_sig_with_fallback!(
        validator,
        &canister_sig,
        &sender_info_content,
        &public_key,
        root_of_trust_provider,
        |e| InvalidSenderInfo(format!("signature verification failed: {e}")),
        |e: <R as RootOfTrustProvider>::Error| InvalidSenderInfo(format!(
            "failed to get root of trust: {e}"
        ))
    );
```

**File:** packages/ic-signature-verification/src/canister_sig.rs (L16-29)
```rust
pub fn verify_canister_sig(
    message: &[u8],
    signature_cbor: &[u8],
    public_key_der: &[u8],
    ic_root_public_key_raw: &[u8],
) -> Result<(), String> {
    let signature = parse_signature_cbor(signature_cbor)?;
    let public_key = CanisterSigPublicKey::try_from(public_key_der)
        .map_err(|e| format!("failed to parse canister sig public key: {e}"))?;
    let certificate =
        check_certified_data_and_get_certificate(&signature, &public_key.canister_id)?;
    check_sig_path(&signature, &public_key, message)?;
    verify_certificate(&certificate, public_key.canister_id, ic_root_public_key_raw)
}
```
