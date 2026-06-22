### Title
`SenderInfoContent` Signature Replay Across Target Canisters — (`File: rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `sender_info` canister signature is verified only over the raw `info` bytes with a static domain separator `"ic-sender-info"`. The signed content does not include the target canister ID, the method name, or any other request-binding context. This allows a user to obtain a single `sender_info` signature from a signing canister (e.g., Internet Identity) and replay it verbatim in requests to any other canister, indefinitely.

---

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over raw bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // only the raw info bytes
    }
}
```

Its `SignatureDomain` implementation uses a static string:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")  // no canister ID, no request context
    }
}
```

So the full signed bytes are: `"\x0Eic-sender-info" || info_bytes`.

The validator in `verify_sender_info_canister_sig` reconstructs exactly this object and verifies the canister signature against it:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
verify_canister_sig_with_fallback!(validator, &canister_sig, &sender_info_content, ...);
```

No field from the enclosing request — not `canister_id`, not `method_name`, not `ingress_expiry` — is mixed into the signed payload. The only binding is to the signing canister's identity (via the canister signature mechanism, which ties the signature to `(canister_id, seed)`).

Because the outer `sender_sig` (over the `MessageId`) is controlled by the user themselves, the user can freely compose any `sender_info` blob they previously obtained and attach it to any new request to any target canister.

---

### Impact Explanation

A user who has obtained a valid `sender_info` attestation from Internet Identity (or any other signing canister) for one purpose can replay that exact `{info, signer, sig}` triple in requests to completely different target canisters. Any canister that reads `msg_caller_info_data()` and makes authorization decisions based on the attested attributes (e.g., KYC status, age verification, role membership) cannot distinguish a freshly issued attestation from a replayed one obtained in a different context or for a different service. The `sender_info.sig` carries no expiry of its own; only the outer `ingress_expiry` limits the request window, but the user can keep issuing fresh outer-signed requests with the same stale `sender_info`.

---

### Likelihood Explanation

The attack requires only that:
1. The user has previously obtained a valid `sender_info` from a signing canister.
2. At least one target canister uses `msg_caller_info_data()` for context-sensitive authorization.

Both conditions are the intended use-case for `sender_info` (Internet Identity issuing user attributes to dapps). The replay requires no privileged access, no key compromise, and no network-level attack. Any user of the system can perform it.

---

### Recommendation

Bind the `sender_info` signature to the target canister ID (and optionally the method name or the full `MessageId`) by including it in the signed content. The ERC1271 fix used EIP-712 with `address(this)` in the domain separator; the IC equivalent is to include the target `canister_id` inside `SenderInfoContent`:

```rust
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // include target canister_id before the info bytes
        bytes.extend_from_slice(self.target_canister_id.as_ref());
        bytes.extend_from_slice(self.info);
    }
}
```

Alternatively, include the full `MessageId` (which already commits to `canister_id`, `method_name`, `arg`, `sender`, and `ingress_expiry`) so the attestation is single-use per request.

---

### Proof of Concept

1. User U (seed `S`, signing canister = Internet Identity `II`) calls canister A with a freshly issued `sender_info = {info: b"kyc_verified", signer: II, sig: σ}`.
2. The replica accepts the request; `verify_sender_info_canister_sig` verifies σ over `"\x0Eic-sender-info" || b"kyc_verified"` under U's public key — no canister A ID is checked.
3. U constructs a second request to canister B (a DeFi dapp that gates withdrawals on `kyc_verified`), reusing the identical `sender_info` triple. U signs the new `MessageId` (which commits to canister B's ID) with their own key.
4. The replica again accepts: `verify_sender_info_canister_sig` reconstructs `SenderInfoContent(b"kyc_verified")` and verifies σ — the same bytes, the same signature, a different target canister. Canister B reads `msg_caller_info_data()`, sees `kyc_verified`, and grants the withdrawal.

The signed bytes at step 2 and step 4 are identical (`"\x0Eic-sender-info" || b"kyc_verified"`), so the same σ satisfies both verifications. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/types/types/src/messages/http.rs (L336-348)
```rust
/// The content bytes of a sender_info field, used as the signable message
/// for canister signature verification of sender info.
///
/// The signing canister (e.g. Internet Identity) signs the `info` blob
/// using a canister signature with the domain separator `"ic-sender-info"`.
#[derive(Clone, Debug)]
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
}
```

**File:** rs/types/types/src/crypto/sign.rs (L161-165)
```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L490-521)
```rust
/// Verifies that the sender_info canister signature is valid:
/// 1. The envelope-level sender_pubkey is a valid canister signature public key.
/// 2. The canister ID encoded in sender_pubkey matches the signer field.
/// 3. The signature over the info blob is valid against the root of trust.
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
