### Title
`SenderInfoContent` Signature Missing Request-Binding Parameters and Expiration — (`rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `SenderInfoContent` signable type signs only the raw `info` bytes, omitting all request-context parameters (`canister_id`, `sender`, `ingress_expiry`). Additionally, `validate_sender_info` performs no expiration check on the canister signature certificate. This directly mirrors the external report's two attack vectors: (1) a parameter-substitution attack where the same attestation is replayed across different target canisters, and (2) a stale-signature attack where a revoked attestation remains usable indefinitely.

---

### Finding Description

`SenderInfoContent` is the signable type used to verify the `sender_info` field in HTTP ingress and query requests. Its `write_signed_bytes_without_domain_separator` implementation writes only the raw `info` blob:

```rust
// rs/types/types/src/messages/http.rs
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // ← only info bytes, no canister_id, sender, expiry
    }
}
``` [1](#0-0) 

The full domain-separated signed bytes therefore become `\x0Eic-sender-info` + `info_bytes` — with no binding to the target `canister_id`, the `sender` principal, or the `ingress_expiry`.

The validator that checks this signature is `verify_sender_info_canister_sig`, which verifies the canister signature over `SenderInfoContent(&sender_info.info)` and checks that the canister ID in `sender_pubkey` matches `sender_info.signer`, but performs no check on the certificate timestamp or any request-context field:

```rust
// rs/validator/src/ingress_validation.rs
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
verify_canister_sig_with_fallback!(validator, &canister_sig, &sender_info_content, ...);
``` [2](#0-1) 

The `validate_sender_info` entry point also performs no expiration check on the certificate: [3](#0-2) 

The `sender_info` field is included in the `MessageId` representation-independent hash (so the envelope-level `sender_sig` is bound to the request), but the inner `sender_info.sig` canister signature is not: [4](#0-3) 

---

### Impact Explanation

**Attack vector 1 — Cross-canister attestation replay (analog to "Period Manipulation"):**

1. A signing canister (e.g., Internet Identity) issues a `sender_info.sig` over `info_bytes = b"role:admin"` for user A's request to canister X.
2. User A reuses the identical `sender_info.sig` in a request to canister Y (a different governance or access-control canister).
3. Canister Y calls `msg_caller_info_data()`, receives `role:admin`, and grants elevated access — even though the signing canister never attested these attributes for canister Y.

Because `canister_id` is absent from `SenderInfoContent`, the same signature is cryptographically valid for any target canister, as long as `info_bytes` and `sender_pubkey` are unchanged.

**Attack vector 2 — Stale attestation replay (analog to "Stale Strike Price"):**

1. Internet Identity certifies `info_bytes` for user A at time T.
2. User A's attributes are later revoked; Internet Identity updates its certified data.
3. User A retains the old certificate-based `sender_info.sig`. Because `verify_certificate` in the canister-signature library verifies only the BLS signature on the certificate and does not compare the certificate timestamp against the current replica time, the old certificate remains accepted.
4. User A submits new ingress messages with the stale `sender_info.sig`; canisters see the revoked attributes as valid. [5](#0-4) 

---

### Likelihood Explanation

**Medium.** The `sender_info` feature is new and actively used (integration tests exist for it). Any canister that calls `msg_caller_info_data()` and makes access-control decisions based on the returned blob is affected. An unprivileged ingress sender can craft a valid request reusing a previously obtained `sender_info.sig` with no special privileges. The attacker only needs to have obtained a valid `sender_info.sig` once (e.g., from a prior legitimate request visible on-chain or shared by the signing canister).

---

### Recommendation

1. **Bind `SenderInfoContent` to the request context.** Include at minimum `canister_id` and `sender` in the signed bytes so the attestation is scoped to a specific target and principal:

```rust
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.canister_id.as_ref());  // add
        bytes.extend_from_slice(self.sender.as_slice());     // add
        bytes.extend_from_slice(self.info);
    }
}
```

2. **Enforce a certificate-age deadline.** In `validate_sender_info`, extract the `time` field from the canister-signature certificate and reject it if it is older than a configurable maximum (e.g., `MAX_INGRESS_TTL`).

---

### Proof of Concept

```
1. Obtain a valid sender_info.sig for info_bytes = b"role:admin" targeting canister X
   (e.g., from a prior legitimate request or by calling the signing canister directly).

2. Construct a new HttpCanisterUpdate targeting canister Y:
   HttpCanisterUpdate {
       canister_id: canister_Y,
       method_name: "privileged_action",
       sender: user_A,
       ingress_expiry: <fresh expiry>,
       sender_info: Some(RawSignedSenderInfo {
           info: b"role:admin",
           signer: II_canister_id,
           sig: <reused sender_info.sig from step 1>,
       }),
       ...
   }

3. Sign the new MessageId with user_A's key (envelope-level signature is fresh and valid).

4. Submit to the replica. validate_sender_info passes because:
   - sender_pubkey encodes (II_canister_id, seed_A) ✓
   - pubkey_canister_id == sender_info.signer ✓
   - SenderInfoContent(b"role:admin") verifies against the reused certificate ✓
   - No canister_id or expiry check is performed ✓

5. Canister Y's msg_caller_info_data() returns b"role:admin" and grants elevated access.
``` [6](#0-5) [7](#0-6)

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

**File:** rs/validator/src/ingress_validation.rs (L494-544)
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
