### Title
`SenderInfoContent` Canister Signature Does Not Bind to Target `canister_id`, Enabling Cross-Canister Replay of Attestations - (File: `rs/types/types/src/messages/http.rs`)

---

### Summary

The `SenderInfoContent` signed-bytes construction omits the target `canister_id` from the signed payload. A valid `sender_info` canister signature obtained for one target canister can be replayed verbatim to any other canister. Any canister that uses `msg_caller_info` for authorization decisions can be deceived into accepting an attestation that was never issued for it.

---

### Finding Description

`SenderInfoContent` is the signable type used to verify the `sender_info` field of an ingress message. Its signed bytes are constructed as:

```
\x0Eic-sender-info  ||  info_bytes
``` [1](#0-0) [2](#0-1) 

The signed payload contains **only** the raw `info` blob. The target `canister_id`, the `sender` principal, the `ingress_expiry`, and the `nonce` are all absent from the signed content.

The replica-side verifier `verify_sender_info_canister_sig` checks three things:

1. `sender_pubkey` is a valid ICCSA public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid. [3](#0-2) 

There is no check that the signature is bound to the specific target canister of the current request. Because the signed bytes are identical regardless of which canister the request targets, a signature produced by the signing canister (e.g., Internet Identity) for a call to canister A is cryptographically indistinguishable from a signature intended for canister B.

This is the direct IC analog of the PayrollManager bug: `validatePayrollTxHashes` signed only `rootHash` without `safeAddress`, allowing replay across different safes. Here, `SenderInfoContent` signs only `info_bytes` without `canister_id`, allowing replay across different canisters.

---

### Impact Explanation

A canister that calls `msg_caller_info` and uses the returned `info` blob for authorization (e.g., "caller has role=admin", "caller is KYC-verified") can be deceived. An attacker who legitimately obtains a `sender_info` signature from a trusted signing canister for one target canister can replay that exact signature to any other canister that trusts the same signing canister. The replaying canister receives a fully valid, replica-verified `sender_info` blob that was never issued for it, potentially granting unauthorized privilege escalation or bypassing access controls.

---

### Likelihood Explanation

The attacker needs only to:

1. Obtain a valid `sender_info` signature from a signing canister for any `info_bytes` of their choosing (a normal, unprivileged operation — the signing canister signs whatever the user requests).
2. Construct a new ingress message targeting a different canister, copying the `sender_info` field verbatim.
3. Sign the new envelope with their own key pair (the envelope signature covers the `MessageId`, which includes the `sender_info` blob, so the envelope signature is fresh and valid).

No privileged access, no key compromise, and no threshold attack is required. The attack is reachable via the standard `/api/v2/canister/{id}/call` endpoint.

---

### Recommendation

Include the target `canister_id` in the `SenderInfoContent` signed bytes, analogous to how the PayrollManager fix included `safeAddress` in the digest:

```rust
// rs/types/types/src/messages/http.rs

pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub canister_id: &'a [u8],   // add target canister binding
}

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.canister_id);
        bytes.extend_from_slice(self.info);
    }
}
```

The `verify_sender_info_canister_sig` call site in `rs/validator/src/ingress_validation.rs` must pass the target `canister_id` from the request content when constructing `SenderInfoContent`. The signing canister (e.g., Internet Identity) must also include the target `canister_id` when producing the signature. [4](#0-3) 

---

### Proof of Concept

**Setup:** Internet Identity canister `II` is trusted by both canister `A` and canister `B`. Both canisters call `msg_caller_info` and grant elevated access when `info_bytes == b"role=admin"`.

**Step 1 — Obtain a valid signature for canister A:**

User `U` (whose principal is `self_authenticating(sender_pubkey)`) calls canister `A` with:
```
sender_info = {
    info:   b"role=admin",
    signer: II_canister_id,
    sig:    II.sign(SenderInfoContent(b"role=admin"))
           = sign(\x0Eic-sender-info || b"role=admin")
}
```
The replica validates the signature. Canister `A` grants admin access.

**Step 2 — Replay to canister B:**

User `U` constructs a new ingress message targeting canister `B`, copying `sender_info` verbatim:
```
sender_info = {
    info:   b"role=admin",
    signer: II_canister_id,
    sig:    <same bytes as above>
}
```
User `U` signs the new envelope with their own key (the `MessageId` changes because `canister_id` in the request body changes, but the `sender_info.sig` is over `info_bytes` only and remains valid).

**Step 3 — Replica accepts:**

`verify_sender_info_canister_sig` verifies:
- `sender_pubkey` encodes `II_canister_id` ✓
- `II_canister_id == sender_info.signer` ✓
- `II.verify(sig, SenderInfoContent(b"role=admin"))` ✓ (signed bytes are identical)

The replica passes `info_bytes = b"role=admin"` to canister `B` via `msg_caller_info`. Canister `B` grants admin access to user `U` — an authorization that `II` never issued for canister `B`. [5](#0-4) [6](#0-5)

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

**File:** rs/validator/src/ingress_validation.rs (L490-544)
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

    // Construct the UserPublicKey for verification.
    let public_key = UserPublicKey {
        key: pk_bytes.0,
        algorithm_id: AlgorithmId::IcCanisterSignature,
    };

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
    Ok(())
```
