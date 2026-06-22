### Title
`sender_info` Canister Signature Is Not Bound to Any Specific Request — Indefinite Replay of Attestations - (File: `rs/validator/src/ingress_validation.rs`)

### Summary

The `sender_info` field in IC ingress messages carries a canister-signed attestation blob (e.g., from Internet Identity). The canister signature is verified only over the raw `info` bytes with the domain separator `ic-sender-info`. It is not bound to the sender principal, the target canister, the `ingress_expiry`, or the message ID. Once a signing canister produces a valid `sender_info` signature, any holder of that `{info, signer, sig}` triple can replay it across an unlimited number of future requests, to any canister, indefinitely. There is no expiry field in the signed content and no revocation mechanism for individual attestations.

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over a raw byte slice: [1](#0-0) 

Its `write_signed_bytes_without_domain_separator` implementation appends only the raw `info` bytes: [2](#0-1) 

The `SignatureDomain` for `SenderInfoContent` prepends only the literal string `ic-sender-info`: [3](#0-2) 

The validator's `verify_sender_info_canister_sig` constructs the signable content as `SenderInfoContent(&sender_info.info)` — containing nothing but the info blob — and verifies the canister signature against it: [4](#0-3) 

The signed bytes are therefore: `\x0Eic-sender-info || info_bytes`. They do **not** include:
- The sender's `UserId` / principal
- The target `CanisterId`
- The `ingress_expiry` timestamp
- The `MessageId` (which would bind the attestation to one specific request)

The `validate_sender_info` function that calls this verifier also does not check any expiry on the attestation itself: [5](#0-4) 

The `ingress_expiry` of the outer envelope is validated separately and limits how long a single ingress message is live (up to `MAX_INGRESS_TTL`), but the `sender_info` canister signature itself carries no expiry and is not consumed or invalidated after use. A user who obtains `{info, signer, sig}` once can attach it to any number of future ingress messages, each with a fresh `ingress_expiry`, indefinitely.

### Impact Explanation

Any canister that reads `sender_info` via `msg_caller_info` and uses it for access-control decisions (e.g., "this user is KYC-verified," "this user has role X") is exposed to stale-attestation replay. If the signing canister (e.g., Internet Identity) later determines that the user's attributes have changed or should be revoked, it has no protocol-level mechanism to invalidate the previously issued `{info, sig}` pair. The user can continue presenting the old attestation in every new ingress message they send, bypassing any attribute-based access control the target canister enforces. Because the `sender_info` signature is not bound to the sender principal either, a user who shares the signed blob with a colluding party allows that party to attach the same attestation to their own messages (the outer envelope signature still requires the colluding party's own key, but the `sender_info` content is accepted regardless of who presents it).

### Likelihood Explanation

The `sender_info` feature is new and explicitly designed for use by identity providers such as Internet Identity. Any canister that relies on `sender_info` for authorization — a natural and intended use case — is immediately affected. An attacker needs only to have legitimately obtained a `sender_info` attestation once; no privileged access, key compromise, or network-level attack is required to replay it.

### Recommendation

Bind the `sender_info` canister signature to request-specific context. At minimum, include the `ingress_expiry` of the outer envelope in the signed content so that the attestation automatically becomes invalid when the message window closes. Ideally, include the full `MessageId` (which commits to sender, canister, method, args, expiry, and nonce) so each attestation is single-use. The `SenderInfoContent` signable should be changed from:

```
ic-sender-info || info_bytes
```

to something like:

```
ic-sender-info || info_bytes || ingress_expiry_nanos
```

or:

```
ic-sender-info || message_id || info_bytes
```

This mirrors the fix applied in zAuction: adding an `expireblock` to the signed bid message so that old signed commitments automatically become invalid after a bounded time window.

### Proof of Concept

1. A signing canister S signs `info = b"role:admin"` producing `sig`. The signed bytes are `\x0Eic-sender-info` + `b"role:admin"`.
2. User U constructs ingress message M1 to canister C1 with `sender_info = {info, signer: S, sig}`, signs M1 with their key, and sends it. The replica accepts it; C1 grants admin access.
3. S later revokes U's admin role (e.g., by updating its internal state). S cannot invalidate the already-issued `sig`.
4. U constructs a new ingress message M2 to C1 (or any other canister) with a fresh `ingress_expiry` but the same `{info, signer: S, sig}`. The replica's `verify_sender_info_canister_sig` verifies the canister signature over `SenderInfoContent(b"role:admin")` — which is still cryptographically valid — and accepts M2. C1 again grants admin access, contrary to S's intent. [6](#0-5) [7](#0-6)

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
