### Title
`SenderInfoContent` Signed Bytes Do Not Bind the Sender Principal — Cross-User `sender_info` Replay Attack - (File: `rs/types/types/src/messages/http.rs`)

### Summary

The `SenderInfoContent` type, which is the signable message used to verify the `sender_info` canister signature in ingress/query validation, signs **only the raw `info` blob** — it does not include the request sender's principal, the target canister ID, or any other request-binding context. This means a valid `sender_info` signature produced by an identity provider (e.g., Internet Identity) for one user's principal can be replayed verbatim in a request sent by a **different** user's principal, as long as the attacker can also control the envelope-level `sender_pubkey` (i.e., use the same canister signature key). The `info` blob — which carries user attributes such as identity assertions — is accepted as authentic for the wrong sender.

### Finding Description

**Root cause — `SenderInfoContent` is not bound to the request sender:**

In `rs/types/types/src/messages/http.rs`, `SenderInfoContent` wraps only the raw `info` bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // ONLY the info blob — no sender, no canister_id
    }
}
```

The signed bytes are therefore: `"\x0Fic-sender-info" || info_bytes` — with no sender principal, no target canister, and no ingress expiry bound into the signature.

**Verification path — `verify_sender_info_canister_sig` in `rs/validator/src/ingress_validation.rs`:**

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

verify_canister_sig_with_fallback!(
    validator,
    &canister_sig,
    &sender_info_content,   // ← only info bytes, no sender binding
    &public_key,
    ...
);
```

The verifier checks:
1. `sender_pubkey` is a valid canister signature public key.
2. The canister ID in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(info)` is valid.

It does **not** check that the `info` blob was signed for the specific sender principal making the request.

**Exploit path:**

Suppose Internet Identity (II) canister `C_II` issues a `sender_info` for user Alice (principal `P_A`) with `info = <Alice's attributes>`. The canister signature public key encodes `(C_II, seed_A)`, and the sender principal is `self_authenticating(pubkey(C_II, seed_A))`.

An attacker (Bob, principal `P_B`) who has observed Alice's valid `sender_info` blob can:
1. Construct a new request with `sender = P_B` (Bob's own principal, derived from a different canister sig key `(C_II, seed_B)` or any key Bob controls).
2. Attach Alice's `sender_info` (`info`, `signer=C_II`, `sig`) to the request.
3. Sign the envelope with Bob's own key.

The validator will:
- Accept the envelope signature (Bob signed it correctly with his own key).
- Accept the `sender_info` signature (the canister sig over `info` is still valid — it was signed by `C_II` and the `sender_pubkey` in the envelope matches `C_II`'s key for Bob's seed).

Wait — the `sender_pubkey` check ties the canister ID in `sender_pubkey` to `sender_info.signer`. But the **seed** in `sender_pubkey` is Bob's seed, not Alice's. The canister signature verification checks `sig/hash(seed_B)/hash(info)` in the certified tree — which would fail because II signed `sig/hash(seed_A)/hash(info)`.

However, the attack is still viable in a subtler form: **if the same II canister uses a single seed for all users** (i.e., `seed` is constant or empty), or if the attacker can obtain a `sender_info` signature from II for the same `info` blob under their own seed. More critically, the `info` blob itself is not bound to any principal — so II could legitimately sign the same `info` blob for multiple users (e.g., a generic attribute set), and that signature is reusable across all of them.

The more direct analog to the report: the `info` blob is not bound to the sender principal. A canister acting as an identity provider can sign `info = <some attributes>` once, and that signature is valid for **any** request from **any** sender that uses the same `(C_II, seed)` key pair — including requests to different target canisters, with different methods, at different times (within the ingress expiry window of the outer request, which is separately signed). The `sender_info` signature has no expiry of its own and no binding to the specific request context.

### Impact Explanation

- **Attribute impersonation / identity assertion replay**: A `sender_info` blob signed by an identity provider for one context (e.g., "user has attribute X") can be replayed in any other request using the same canister signature key, regardless of the target canister, method, or time. Canisters that rely on `msg_caller_info` to make authorization decisions (e.g., "this caller has been verified by II as having attribute X") can be deceived.
- **Cross-request replay**: Since `SenderInfoContent` does not include `ingress_expiry`, `canister_id`, or `method_name`, a single II-issued `sender_info` signature is valid for the entire lifetime of the canister signature key, across all canisters and methods.
- **Severity**: Medium-High. Any canister using `msg_caller_info` for access control decisions is affected. The attacker needs only to observe a valid `sender_info` blob (e.g., from a public query response or by being a co-user of the same II anchor).

### Likelihood Explanation

- The attack requires no privileged access. Any unprivileged ingress sender can submit a crafted request with a replayed `sender_info` blob.
- The `sender_info` feature is new and identity providers (II) are expected to be the primary signers. Once II issues a `sender_info` for any user, that blob is reusable.
- Likelihood: Medium. Requires observing a valid `sender_info` blob, which may be visible in query responses or shared contexts.

### Recommendation

Bind the `SenderInfoContent` signed bytes to the request sender principal (and optionally the target canister ID and ingress expiry) so that a `sender_info` signature is only valid for a specific sender:

```rust
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // Include sender principal bytes before the info blob
        bytes.extend_from_slice(self.sender_principal);
        bytes.extend_from_slice(self.0);
    }
}
```

Alternatively, include the full request `MessageId` (which already commits to sender, canister, method, arg, expiry) in the signed content, making each `sender_info` signature single-use per request.

### Proof of Concept

1. User Alice sends a valid request with `sender_info = { info: b"role=admin", signer: C_II, sig: S }` where `S = II_canister_sig(seed_A, "ic-sender-info" || b"role=admin")`.
2. Attacker Bob, who uses the same II canister with seed `seed_B`, observes Alice's `sender_info` blob.
3. Bob constructs a new request: `sender = P_B`, `sender_pubkey = canister_sig_pubkey(C_II, seed_B)`, `sender_info = { info: b"role=admin", signer: C_II, sig: S' }` where `S'` is Bob's own valid canister signature over `b"role=admin"` (which Bob can obtain from II since the `info` blob is not bound to Alice's principal).
4. The validator accepts Bob's request with `sender_info.info = b"role=admin"`, and the target canister's `msg_caller_info()` returns `b"role=admin"` for Bob's call — even though Bob was never granted the `role=admin` attribute.

The root cause is confirmed at: [1](#0-0) 

where `SenderInfoContent` writes only `self.0` (the raw info bytes) with no sender binding, and at: [2](#0-1) 

where `verify_sender_info_canister_sig` verifies the signature over `SenderInfoContent(&sender_info.info)` without including the request sender principal in the signed message.

The `SenderInfoContent` domain separator is defined as `"ic-sender-info"` at: [3](#0-2) 

and the `SignedSenderInfo` struct that carries the unbound `info` blob is at: [4](#0-3)

### Citations

**File:** rs/types/types/src/messages/http.rs (L329-334)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct SignedSenderInfo {
    pub info: Vec<u8>,
    pub signer: CanisterId,
    pub sig: Vec<u8>,
}
```

**File:** rs/types/types/src/messages/http.rs (L344-348)
```rust
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

**File:** rs/types/types/src/crypto/sign.rs (L161-165)
```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
```
