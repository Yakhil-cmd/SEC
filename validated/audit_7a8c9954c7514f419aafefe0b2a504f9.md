### Title
`SenderInfoContent` Canister Signature Does Not Bind to Request Context, Enabling Cross-Canister Replay - (File: `rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `sender_info` field in IC ingress messages allows a canister (e.g., Internet Identity) to attest user attributes by signing an `info` blob. The signed payload — `SenderInfoContent` — contains **only the raw `info` bytes** with domain separator `"ic-sender-info"`. It does not include the target `canister_id`, `method_name`, `arg`, `sender`, or `ingress_expiry`. As a result, a valid `sender_info` canister signature obtained for one request is cryptographically valid for **any other request** from the same sender to any canister, as long as the same `info` bytes are used. This is a direct analog of the external report's signature-does-not-cover-all-parameters vulnerability.

---

### Finding Description

`SenderInfoContent` is defined as a wrapper over the raw `info` bytes only: [1](#0-0) 

The `write_signed_bytes_without_domain_separator` implementation appends only `self.0` (the raw `info` bytes) to the signed payload. No request-specific context is included.

The `SignatureDomain` for `SenderInfoContent` adds only the `"ic-sender-info"` domain separator: [2](#0-1) 

During validation, `verify_sender_info_canister_sig` constructs the signable content as `SenderInfoContent(&sender_info.info)` and verifies the canister signature against it: [3](#0-2) 

The full validation flow in `validate_sender_info` extracts the `sender_pubkey` from the envelope and calls `verify_sender_info_canister_sig`, but never passes any request-specific fields (canister_id, method_name, arg, ingress_expiry) into the signed payload: [4](#0-3) 

The `MessageId` (which covers all request fields including `canister_id`, `method_name`, `arg`, `sender`, `ingress_expiry`, and `nonce`) is verified separately as the envelope signature, but is **not** included in the `sender_info` canister signature: [5](#0-4) 

---

### Impact Explanation

A signing canister (e.g., Internet Identity) that certifies `info = b"role=admin"` for a user's canister-signature key produces a canister signature over `"ic-sender-info" || info`. This signature is valid for **every** request the user sends — to any canister, calling any method, with any arguments — as long as the same `info` bytes are used. The signing canister has no protocol-level mechanism to restrict the attestation to a specific target canister, method, or time window.

Concretely:
- If canister A grants admin access based on `sender_info.info == b"role=admin"`, and a user obtains this attestation from II for canister A, the same `(info, sig)` pair is accepted by canister B, canister C, etc.
- Canisters that use `sender_info` for access control cannot distinguish whether the attestation was intended for them or for a different canister.
- The `ingress_expiry` field of the outer request is validated separately (and limits the replay window to `MAX_INGRESS_TTL`), but the `sender_info` signature itself has no expiry binding — the same canister signature remains valid indefinitely as long as the canister's certified state is not updated. [6](#0-5) 

---

### Likelihood Explanation

The `sender_info` feature is implemented in production ingress validation code and is reachable by any unprivileged external sender submitting an HTTP call or query. The attack path requires only:
1. Obtaining a valid `sender_info` attestation from a signing canister (a normal user action).
2. Reusing the same `(info, sig)` pair in requests to different target canisters (trivially constructable by any user).

No privileged access, key compromise, or threshold attack is required. The attacker is the same principal (same `sender_pubkey`), so the replay is self-replay across canister boundaries — but this is precisely the scenario the `sender_info` mechanism is supposed to prevent (context-specific attestation).

---

### Recommendation

The `SenderInfoContent` signed payload should include request-specific context to bind the attestation to the intended target. At minimum, the `canister_id` of the target canister should be included in the signed bytes:

```rust
// Proposed: bind sender_info to the target canister
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub canister_id: &'a [u8],  // target canister
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.canister_id);
        bytes.extend_from_slice(self.info);
    }
}
```

Alternatively, binding to the full `MessageId` (which already covers all request fields) would provide the strongest guarantee. The `validate_sender_info` function would need to pass the request's `canister_id` (or `MessageId`) into `verify_sender_info_canister_sig`.

---

### Proof of Concept

1. User Alice obtains a valid `sender_info` from Internet Identity for `info = b"role=admin"`, producing `sig = II.sign(SenderInfoContent(b"role=admin"))`.
2. Alice sends request R1 to canister A (`method = "admin_action"`) with `sender_info = {info: b"role=admin", signer: II, sig: sig}`. Canister A grants admin access.
3. Alice constructs request R2 to canister B (`method = "privileged_action"`) with the **identical** `sender_info = {info: b"role=admin", signer: II, sig: sig}` and a fresh `ingress_expiry`.
4. The IC validator calls `verify_sender_info_canister_sig` for R2, constructs `SenderInfoContent(b"role=admin")`, and verifies `sig` — which succeeds, because the signed payload is identical to what II certified.
5. Canister B receives the request with `msg_caller_info_data() == b"role=admin"` and grants admin access, even though II never attested Alice's role for canister B.

The root cause is confirmed at: [7](#0-6) [8](#0-7)

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

**File:** rs/validator/src/ingress_validation.rs (L547-585)
```rust
// Check if ingress_expiry is within a proper range with respect to the given
// time, i.e., it is not expired yet and is not too far in the future.
fn validate_ingress_expiry<C: HttpRequestContent>(
    request: &HttpRequest<C>,
    current_time: Time,
) -> Result<(), RequestValidationError> {
    let ingress_expiry = request.ingress_expiry();
    let provided_expiry = Time::from_nanos_since_unix_epoch(ingress_expiry);
    let min_allowed_expiry = current_time;
    // We need to account for time drift and be more forgiving at rejecting ingress
    // messages due to their expiry being too far in the future.
    // If this logic changes, then the migration canister in `//rs/migration_canister`
    // must be updated, too.
    let max_expiry_diff = MAX_INGRESS_TTL
        .checked_add(PERMITTED_DRIFT_AT_VALIDATOR)
        .ok_or_else(|| {
            InvalidRequestExpiry(format!(
                "Addition of MAX_INGRESS_TTL {MAX_INGRESS_TTL:?} with \
                PERMITTED_DRIFT_AT_VALIDATOR {PERMITTED_DRIFT_AT_VALIDATOR:?} overflows",
            ))
        })?;
    let max_allowed_expiry = min_allowed_expiry
        .checked_add(max_expiry_diff)
        .ok_or_else(|| {
            InvalidRequestExpiry(format!(
                "Addition of min_allowed_expiry {min_allowed_expiry:?} \
                with max_expiry_diff {max_expiry_diff:?} overflows",
            ))
        })?;
    if !(min_allowed_expiry <= provided_expiry && provided_expiry <= max_allowed_expiry) {
        let msg = format!(
            "Specified ingress_expiry not within expected range: \
             Minimum allowed expiry: {min_allowed_expiry}, \
             Maximum allowed expiry: {max_allowed_expiry}, \
             Provided expiry:        {provided_expiry}"
        );
        return Err(InvalidRequestExpiry(msg));
    }
    Ok(())
```

**File:** rs/types/types/src/messages/ingress_messages.rs (L112-130)
```rust
impl HttpRequestContent for SignedIngressContent {
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
