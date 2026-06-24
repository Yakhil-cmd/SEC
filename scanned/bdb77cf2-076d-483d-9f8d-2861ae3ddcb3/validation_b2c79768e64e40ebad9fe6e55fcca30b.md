### Title
`sender_info` Canister Signature Not Bound to Request Context Enables Cross-Request Replay — (`rs/validator/src/ingress_validation.rs`, `rs/types/types/src/messages/http.rs`)

---

### Summary

The `sender_info` canister signature (used to carry user attributes from a signing canister such as Internet Identity) is verified only over the raw `info` blob, with no binding to the specific request's `canister_id`, `method_name`, `arg`, `sender`, or `ingress_expiry`. A user who obtains a valid `sender_info` attestation for one request can freely replay the same `(info, signer, sig)` triple in any other request to any canister, indefinitely. Canisters that rely on `msg_caller_info` for authorization decisions cannot distinguish a fresh, context-specific attestation from a replayed one.

---

### Finding Description

`SenderInfoContent` in `rs/types/types/src/messages/http.rs` is defined as a thin wrapper over the raw `info` bytes only:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // only the info blob
    }
}
``` [1](#0-0) 

The signed message is therefore `"ic-sender-info" || info` — nothing else. It does not include `canister_id`, `method_name`, `arg`, `sender`, or `ingress_expiry`.

In `verify_sender_info_canister_sig`, the validator constructs the signable content from only `sender_info.info` and verifies the canister signature against it:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

verify_canister_sig_with_fallback!(
    validator,
    &canister_sig,
    &sender_info_content,   // ← only the info blob, no request context
    &public_key,
    root_of_trust_provider,
    ...
);
``` [2](#0-1) 

The outer envelope signature (`sender_sig`) does cover `sender_info` content because `sender_info` is included in the `MessageId` (representation-independent hash):

```rust
if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
    map.insert("sender_info", Map(btreemap! {
        "info" => Bytes(info),
        "signer" => Bytes(signer),
        "sig" => Bytes(sig),
    }));
}
``` [3](#0-2) 

This means a third party cannot steal and replay a `sender_info` in their own request (they lack the user's private key for the envelope). However, the **legitimate user** can reuse the same `(info, signer, sig)` triple across an unlimited number of different requests — to different canisters, different methods, different arguments — simply by signing a new envelope each time. The signing canister (Internet Identity) has no way to restrict the attestation to a specific call context.

---

### Impact Explanation

Any canister that reads `msg_caller_info` and uses the `info` blob for authorization (e.g., "only users with attribute X may call this method", KYC status, age verification, session-scoped permissions) is vulnerable to cross-request replay:

1. User obtains `sender_info = {info: [attributes], signer: II_canister, sig: S}` for a legitimate call to canister A.
2. User reuses the identical `sender_info` in a request to canister B (a different canister, method, or argument set), signing a fresh envelope.
3. Canister B's `msg_caller_info` returns `[attributes]` as if freshly attested for that specific call.
4. Canister B grants access or takes action based on attributes that were never attested for this context.

The `sender_info.sig` has no protocol-level expiry independent of the signing canister's certified state. As long as the signing canister does not actively revoke the certification, the signature remains valid indefinitely and can be replayed across any number of requests.

---

### Likelihood Explanation

The attack requires only that:
- A canister uses `msg_caller_info` for authorization decisions (the feature was explicitly designed for this use case, e.g., Internet Identity attesting user attributes to dapps).
- The user has obtained any valid `sender_info` from the signing canister.

No privileged access, key compromise, or network-level attack is required. The user themselves is the attacker (self-replay). This is a realistic scenario for any dapp that uses `sender_info`-based attribute gating.

---

### Recommendation

Bind the `sender_info` canister signature to the request context by including the `MessageId` (or at minimum `canister_id` + `ingress_expiry`) in the signed content:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub message_id: &'a [u8],  // bind to the specific request
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.info);
        bytes.extend_from_slice(self.message_id);
    }
}
```

This mirrors the fix recommended in the EVM report: bind the per-item signature to the complete payload hash to prevent partial/cross-context replay.

---

### Proof of Concept

1. User U calls Internet Identity to obtain a canister signature over `info = b"user_is_kyc_verified"`. This produces `sender_info = {info, signer: II_canister_id, sig: S}`.

2. User U sends a legitimate request to canister A (e.g., a DeFi protocol) with this `sender_info`. Canister A checks `msg_caller_info()` and grants access.

3. User U constructs a completely different request to canister B (e.g., a governance canister), with different `canister_id`, `method_name`, and `arg`, but attaches the **identical** `sender_info = {info, signer: II_canister_id, sig: S}`.

4. The replica validates the envelope signature (which covers the new `MessageId` including the replayed `sender_info` bytes) — this passes because U signed the new envelope with their own key.

5. `verify_sender_info_canister_sig` verifies `S` over `SenderInfoContent(b"user_is_kyc_verified")` — this also passes because `S` is a valid canister signature over that blob, regardless of which canister or method is being called.

6. Canister B's `msg_caller_info()` returns `b"user_is_kyc_verified"` and grants access to U, even though Internet Identity never attested these attributes for this specific call to canister B.

The root cause is at: [1](#0-0) [2](#0-1)

### Citations

**File:** rs/types/types/src/messages/http.rs (L68-77)
```rust
    if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
        map.insert(
            "sender_info",
            Map(btreemap! {
                "info" => Bytes(info),
                "signer" => Bytes(signer),
                "sig" => Bytes(sig),
            }),
        );
    }
```

**File:** rs/types/types/src/messages/http.rs (L342-348)
```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
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
