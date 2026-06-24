### Title
`sender_info` Canister Signature Not Bound to Sender Principal Enables Cross-User Attribute Replay - (`rs/validator/src/ingress_validation.rs`, `rs/types/types/src/messages/http.rs`)

### Summary

The `sender_info` canister signature verified in `verify_sender_info_canister_sig` signs only the raw `info` bytes under the domain separator `"ic-sender-info"`, with no binding to the sender principal, target canister, method, or any request-specific context. Any authenticated user of the same signing canister (e.g., Internet Identity) can capture a valid `(info, signer, sig)` triple from an observed request and replay it verbatim in their own requests, causing the target canister to receive a different caller principal paired with a victim's attested `sender_info` attributes.

### Finding Description

**Root cause — `SenderInfoContent` signs only the raw info blob:**

`SenderInfoContent` is defined as a newtype over `&[u8]` whose `write_signed_bytes_without_domain_separator` appends only the raw `info` bytes: [1](#0-0) 

The `SignatureDomain` implementation prepends only the fixed string `"ic-sender-info"`: [2](#0-1) 

So the signed message is exactly `"\x0Eic-sender-info" || info_bytes` — no sender principal, no target canister ID, no ingress expiry, no nonce.

**Validation path — `verify_sender_info_canister_sig`:** [3](#0-2) 

The validator checks:
1. `sender_pubkey` is a canister signature public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(info_bytes)` is valid.

None of these checks bind the `sender_info.sig` to the specific sender principal or request.

**Exploit path:**

1. Internet Identity (II) canister signs `info_bytes` (e.g., `{user_id: 42, role: "admin"}`) for user A using seed S1. User A submits request R1 with `sender_info = (info_bytes, II, sig_S1)`. The `sig_S1` is a canister signature from II over `"\x0Eic-sender-info" || info_bytes`.

2. Attacker (user B, authenticated to II with seed S2) observes R1 on-chain and extracts the `(info_bytes, II, sig_S1)` triple.

3. Attacker constructs request R2 to any canister with:
   - `sender` = user B's principal (derived from II canister sig key, seed S2)
   - `sender_pubkey` = II canister sig key with seed S2
   - `sender_info` = `(info_bytes, II, sig_S1)` — replayed from R1

4. Validation in `verify_sender_info_canister_sig` passes:
   - `sender_pubkey` canister ID = II = `sender_info.signer` ✓
   - `sig_S1` is a valid canister signature from II over `info_bytes` ✓
   - Outer `sender_sig` is valid (user B controls seed S2) ✓

5. The target canister receives R2 with `msg_caller()` = user B's principal but `msg_caller_info_data()` = user A's attested attributes. Any authorization logic that trusts `sender_info` to gate privileged actions is bypassed.

The `sender_info` is included in the `MessageId` hash (preventing the outer envelope from being replayed), but the inner `sender_info.sig` itself is freely reusable across any number of distinct requests by any II user. [4](#0-3) 

### Impact Explanation

Any canister that reads `msg_caller_info_data()` and uses the attested `info` bytes for authorization decisions (e.g., role checks, KYC status, age verification) can be deceived into granting user B the privileges attested for user A. The attacker only needs to be a legitimate user of the same signing canister (e.g., any II account) and to have observed one valid request from the victim. The victim suffers unauthorized privilege escalation by the attacker; the attacker gains whatever access the victim's `sender_info` attributes confer.

### Likelihood Explanation

The `sender_info` feature is production code in the IC validator and type system. Any canister that adopts `msg_caller_info_data()` for access control is immediately vulnerable. An attacker needs only: (a) an account on the same signing canister as the victim, and (b) one observed request from the victim (observable from the public boundary node API). No privileged access, no key compromise, and no threshold corruption is required.

### Recommendation

Bind the `sender_info` canister signature to the sender principal and/or the full request context. The minimal fix is to include the sender principal in the signed message:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub sender: &'a [u8],  // sender principal bytes
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // length-prefix each field to avoid ambiguity
        bytes.extend_from_slice(&(self.sender.len() as u32).to_be_bytes());
        bytes.extend_from_slice(self.sender);
        bytes.extend_from_slice(self.info);
    }
}
```

Alternatively, bind to the full `MessageId` (which already commits to canister_id, method, arg, ingress_expiry, sender, nonce, and the sender_info triple itself), making the `sender_info.sig` a one-time-use credential per request.

### Proof of Concept

1. Deploy a canister that reads `msg_caller_info_data()` and grants admin access if `info == b"admin"`.
2. As user A (II account, seed S1), submit an update call with `sender_info = (info=b"admin", signer=II, sig=II_sign(b"admin"))`. The canister grants admin access.
3. As user B (II account, seed S2), capture `(info=b"admin", signer=II, sig)` from the boundary node's public request log.
4. As user B, submit a new update call to the same canister with the replayed `sender_info`. Use user B's own II canister sig key (seed S2) for the outer `sender_sig`.
5. `verify_sender_info_canister_sig` passes (II canister ID matches, canister sig over `b"admin"` is valid). The canister grants user B admin access despite II never attesting `b"admin"` for user B.

### Citations

**File:** rs/types/types/src/messages/http.rs (L43-79)
```rust
pub(crate) fn representation_independent_hash_call_or_query(
    request_type: CallOrQuery,
    canister_id: &[u8],
    method_name: &str,
    arg: &[u8],
    ingress_expiry: u64,
    sender: &[u8],
    nonce: Option<&[u8]>,
    sender_info: Option<RawSignedSenderInfoSlices<'_>>,
) -> [u8; 32] {
    use RawHttpRequestVal::*;
    let mut map = btreemap! {
        "request_type" => match request_type {
            CallOrQuery::Call => String("call"),
            CallOrQuery::Query => String("query"),
        },
        "canister_id" => Bytes(canister_id),
        "method_name" => String(method_name),
        "arg" => Bytes(arg),
        "ingress_expiry" => U64(ingress_expiry),
        "sender" => Bytes(sender),
    };
    if let Some(some_nonce) = nonce {
        map.insert("nonce", Bytes(some_nonce));
    }
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
    hash_of_map(&map, |key, value| hash_key_val(key, value))
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
