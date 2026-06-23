### Title
`sender_info` Canister Signature Replay Attack — (`rs/validator/src/ingress_validation.rs`)

---

### Summary

The `sender_info` field in IC ingress messages uses a canister signature over only the raw `info` bytes (with domain separator `"ic-sender-info"`), without binding the signature to any request-specific context. Because canister signatures (ICCSA) are self-contained certificates that remain valid after the signing canister updates its state, a user who obtains a valid `sender_info` signature can replay it indefinitely in new requests — even after the signing canister (e.g., Internet Identity) has revoked the user's attested attributes.

---

### Finding Description

The `verify_sender_info_canister_sig` function in `rs/validator/src/ingress_validation.rs` verifies a canister signature over `SenderInfoContent(&sender_info.info)`: [1](#0-0) 

`SenderInfoContent` is defined as a thin wrapper over the raw `info` bytes: [2](#0-1) 

Its `SignatureDomain` implementation uses the fixed domain separator `"ic-sender-info"`: [3](#0-2) 

The signed content therefore consists of **only** `"ic-sender-info" || info_bytes`. It does **not** include:
- The sender's principal / public key seed
- The target canister ID
- The `ingress_expiry`
- Any per-request nonce

The `validate_sender_info` function extracts the `sender_pubkey` from the envelope and checks that the canister ID encoded in it matches `sender_info.signer`, but this check only ensures the correct canister issued the signature — it does not bind the signature to the specific request: [4](#0-3) 

Because ICCSA canister signatures are self-contained certificates (they embed a subnet-signed state-tree path proving the hash existed in the canister's state at signing time), they remain cryptographically valid even after the signing canister removes the hash from its current certified state. The `verify_canister_sig_with_fallback!` macro performs no expiry check on the embedded certificate: [5](#0-4) 

**Attack scenario:**

1. User A obtains a `sender_info` signature from Internet Identity (II) attesting `info = [KYC_VERIFIED]`.
2. II revokes user A's KYC status by updating its canister state (removing the hash from its certified state tree).
3. User A constructs a new ingress message with a fresh `ingress_expiry` but the same `sender_info` (same `info` blob + same `sig`).
4. The replica's `verify_sender_info_canister_sig` accepts the old signature because the embedded certificate still proves the hash existed at signing time.
5. The target canister reads `msg_caller_info_data()` and sees the revoked attributes, granting access it should deny.

The `sender_info` (including `sig`) is hashed into the `MessageId`: [6](#0-5) 

This means each replayed request gets a distinct `MessageId` (due to differing `ingress_expiry`), bypassing the ingress deduplication mechanism.

---

### Impact Explanation

Any canister that uses `msg_caller_info_data()` / `msg_caller_info_signer()` for access-control decisions (e.g., KYC gating, role-based access, attribute-based authorization) cannot effectively revoke previously issued `sender_info` attestations. A user whose attributes have been revoked by the signing canister can continue to present the old signature in new requests for as long as they retain the original `(info, sig)` pair — indefinitely, since there is no expiry on the canister signature itself.

---

### Likelihood Explanation

The `sender_info` feature is production-targeted (tested in `rs/tests/crypto/ingress_verification_test.rs` against live nodes). The attack requires only that:
1. A canister uses `sender_info` for access control (the stated use case for the feature).
2. The signing canister (e.g., II) revokes a user's attributes.
3. The user retains a previously obtained `(info, sig)` pair.

All three conditions are realistic in any deployment that uses `sender_info` for attribute-based authorization with revocation semantics.

---

### Recommendation

Bind the `sender_info` canister signature to request-specific context so that a signature issued for one request cannot be replayed in another. The signed content should include at minimum the sender's principal (or the full canister-signature public key seed) and the `ingress_expiry`, analogous to how EIP-2612 nonces prevent permit replay:

```rust
// Proposed: bind to sender identity and expiry
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub sender: &'a [u8],       // sender principal bytes
    pub ingress_expiry: u64,    // ties signature to a specific time window
}
```

Alternatively, the signing canister could embed a monotonically increasing nonce in the `info` blob itself and document that canisters must treat `sender_info` as single-use.

---

### Proof of Concept

```
1. Obtain a valid sender_info from II for info = b"kyc_verified":
   sig = II.sign("ic-sender-info" || b"kyc_verified")

2. Use it in request R1 (ingress_expiry = T1) to canister C → accepted.

3. II revokes the user (removes hash from certified state).

4. Construct request R2 with ingress_expiry = T2 > T1, same sender_info:
   {info: b"kyc_verified", signer: II_canister_id, sig: <same sig>}

5. verify_sender_info_canister_sig passes because the ICCSA certificate
   in `sig` still proves the hash existed at T1, and no expiry is checked.

6. Canister C reads msg_caller_info_data() → b"kyc_verified" → grants access.
```

The `sender_info` signature is verified solely against `SenderInfoContent(&sender_info.info)` with no request binding, making step 4–6 unconditionally succeed within any valid `ingress_expiry` window. [7](#0-6) [8](#0-7) [3](#0-2)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L439-457)
```rust
macro_rules! verify_canister_sig_with_fallback {
    ($validator:expr, $sig:expr, $message:expr, $pk:expr, $root_provider:expr,
     $map_verify_err:expr, $map_root_err:expr) => {{
        let verified_with_additional =
            $root_provider
                .additional_root_of_trust()
                .is_some_and(|additional_root_of_trust| {
                    $validator
                        .verify_canister_sig($sig, $message, $pk, &additional_root_of_trust)
                        .is_ok()
                });
        if !verified_with_additional {
            let root_of_trust = $root_provider.root_of_trust().map_err($map_root_err)?;
            $validator
                .verify_canister_sig($sig, $message, $pk, &root_of_trust)
                .map_err($map_verify_err)?;
        }
    }};
}
```

**File:** rs/validator/src/ingress_validation.rs (L459-488)
```rust
fn validate_sender_info<C: HttpRequestContent, R: RootOfTrustProvider>(
    request: &HttpRequest<C>,
    ingress_signature_verifier: &dyn IngressSigVerifier,
    root_of_trust_provider: &R,
) -> Result<(), RequestValidationError>
where
    R::Error: std::error::Error,
{
    let Some(sender_info) = request.sender_info() else {
        return Ok(());
    };

    // Per the spec, the sender_info signature must verify using the
    // envelope-level sender_pubkey as a canister signature public key.
    let sender_pubkey = match request.authentication() {
        Authentication::Authenticated(sig) => &sig.signer_pubkey,
        Authentication::Anonymous => {
            return Err(InvalidSenderInfo(
                "sender_info requires an authenticated request with sender_pubkey".to_string(),
            ));
        }
    };

    verify_sender_info_canister_sig(
        sender_info,
        sender_pubkey,
        ingress_signature_verifier,
        root_of_trust_provider,
    )
}
```

**File:** rs/validator/src/ingress_validation.rs (L494-545)
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
}
```

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

**File:** rs/types/types/src/messages/ingress_messages.rs (L113-130)
```rust
    fn id(&self) -> MessageId {
        MessageId::from(representation_independent_hash_call_or_query(
            CallOrQuery::Call,
            self.canister_id.as_ref(),
            &self.method_name,
            &self.arg,
            self.ingress_expiry,
            self.sender.get_ref().as_slice(),
            self.nonce.as_deref(),
            self.sender_info
                .as_ref()
                .map(|sender_info| RawSignedSenderInfoSlices {
                    info: &sender_info.info,
                    signer: sender_info.signer.as_ref(),
                    sig: &sender_info.sig,
                }),
        ))
    }
```
