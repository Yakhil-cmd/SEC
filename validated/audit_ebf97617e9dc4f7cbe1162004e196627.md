### Title
`SenderInfoContent` Signature Not Bound to Target Canister or Request Context Enables Cross-Canister Replay - (File: `rs/types/types/src/messages/http.rs`)

### Summary

The `SenderInfoContent` signed bytes include only the raw `info` blob under the domain separator `"ic-sender-info"`. No target canister ID, sender principal, ingress expiry, or any other request-specific field is committed to in the signed data. A user who has obtained a valid `sender_info` canister signature (e.g., from Internet Identity) can replay that exact `{info, signer, sig}` triple in any subsequent request — to a different canister, with different arguments, or after their attributes have been revoked — and the replica will accept it as freshly attested.

### Finding Description

`SenderInfoContent` is the signable type used to verify the `sender_info` field of an ingress request. Its signed-bytes implementation is:

```rust
// rs/types/types/src/messages/http.rs:344-348
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // only the raw info blob
    }
}
``` [1](#0-0) 

The full signed bytes are therefore `"\x0Eic-sender-info" || info_bytes` — nothing else. [2](#0-1) 

The validator constructs the signable content from only `sender_info.info` and verifies it:

```rust
// rs/validator/src/ingress_validation.rs:530-531
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
``` [3](#0-2) 

The three checks performed are: (1) `sender_pubkey` is a valid canister sig key, (2) the canister ID in `sender_pubkey` matches `sender_info.signer`, (3) the canister signature over `SenderInfoContent` is valid. None of these checks bind the signature to:

- The **target canister ID** (`canister_id` field of the request)
- The **sender principal** (user ID)
- The **ingress expiry** or any nonce
- Any other request-specific context [4](#0-3) 

By contrast, the `Delegation` signed bytes include `pubkey`, `expiration`, and optionally `targets` — providing request-scoped binding. `SenderInfoContent` has no equivalent binding. [5](#0-4) 

### Impact Explanation

Any canister that calls `msg_caller_info_data()` / `msg_caller_info_signer()` to make authorization decisions (e.g., "this user holds role X, grant elevated access") is vulnerable to receiving a replayed attestation. Concretely:

1. User A authenticates with Internet Identity (II) and obtains a `sender_info` signature attesting `{role: "admin"}`.
2. User A sends a valid request to Canister X; the `{info, signer, sig}` triple is observable on-chain.
3. User A's admin role is subsequently revoked in II.
4. User A constructs a new request to Canister Y (or Canister X with different arguments) and includes the same `{info, signer, sig}` triple.
5. The replica accepts the replayed `sender_info` as valid — the revocation is invisible to Canister Y.

Additionally, a `sender_info` signature obtained for Canister A is equally valid for Canister B, so a signing canister cannot scope its attestation to a specific target. [6](#0-5) 

### Likelihood Explanation

The `sender_info` feature is production-deployed and reachable by any unprivileged ingress sender. The attack requires only that the user previously obtained a valid `sender_info` signature (a normal operation). No privileged access, key compromise, or subnet-majority corruption is needed. The replay is self-contained: the attacker is the same principal reusing their own prior signature. Likelihood is **medium** — the feature is new and canister adoption is growing, but the window of exploitation depends on whether target canisters make security-sensitive decisions based on `sender_info`.

### Recommendation

Bind the `SenderInfoContent` signed bytes to at least the target canister ID and the sender principal, analogous to an EIP-712 domain separator. For example:

```rust
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // Commit to: canister_id || sender || info_bytes
        bytes.extend_from_slice(self.canister_id.as_ref());
        bytes.extend_from_slice(self.sender.as_ref());
        bytes.extend_from_slice(self.info);
    }
}
```

This ensures a `sender_info` signature is valid only for the specific `(signing_canister, target_canister, sender, info)` tuple, preventing cross-canister and cross-request replay.

### Proof of Concept

```
1. Obtain a valid sender_info signature S over info_bytes B from II for canister C_A:
   signed_bytes = "\x0Eic-sender-info" || B
   S = II.canister_sign(signed_bytes)

2. Send request R1 to canister C_A with sender_info = {info: B, signer: II, sig: S}.
   → Accepted by replica. C_A sees info B as freshly attested.

3. Construct request R2 to canister C_B (different canister, same sender, same info):
   sender_info = {info: B, signer: II, sig: S}   // identical triple
   envelope_sig = sign(message_id(R2))            // new envelope sig over R2's MessageId

4. Send R2 to replica.
   → verify_sender_info_canister_sig checks:
     (a) sender_pubkey is valid canister sig key ✓
     (b) canister_id in sender_pubkey == sender_info.signer ✓
     (c) S is valid canister sig over "\x0Eic-sender-info" || B ✓  (same bytes, same sig)
   → Accepted. C_B sees info B as if II freshly attested it for this request.

5. Even after II revokes the user's attributes, S remains cryptographically valid
   and the replica has no mechanism to detect the revocation.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/types/types/src/messages/http.rs (L109-115)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
#[cfg_attr(test, derive(Arbitrary))]
pub struct RawSignedSenderInfo {
    pub info: Blob,
    pub signer: Blob,
    pub sig: Blob,
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

**File:** rs/types/types/src/messages/http.rs (L595-611)
```rust
impl SignedBytesWithoutDomainSeparator for Delegation {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        use RawHttpRequestVal::*;

        let mut map = btreemap! {
            "pubkey" => Bytes(self.pubkey.0.as_slice()),
            "expiration" => U64(self.expiration.as_nanos_since_unix_epoch()),
        };
        if let Some(targets) = &self.targets {
            map.insert(
                "targets",
                Array(targets.iter().map(|t| Bytes(t.0.as_slice())).collect()),
            );
        }

        bytes.extend_from_slice(&hash_of_map(&map, |key, value| hash_key_val(key, value)));
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
