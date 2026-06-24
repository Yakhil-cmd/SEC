### Title
Missing Request Binding in `SenderInfoContent` Allows `sender_info` Signature Replay - (File: `rs/types/types/src/messages/http.rs`)

### Summary

`SenderInfoContent`, the signable type used to verify the `sender_info` canister signature in ingress and query requests, hashes only the raw `info` bytes with a fixed domain separator. It includes no message ID, no canister ID, no nonce, and no expiry. This means a valid `sender_info` canister signature produced by a signing canister (e.g., Internet Identity) for one request can be replayed verbatim in any other request from the same sender, including after the signing canister has revoked or changed the attested attributes.

---

### Finding Description

The `sender_info` field in IC HTTP requests carries a canister-signed attestation about the caller. The signable type used for this attestation is `SenderInfoContent`:

```rust
// rs/types/types/src/messages/http.rs
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // only the raw info bytes
    }
}
``` [1](#0-0) 

Its `SignatureDomain` implementation prepends only the fixed string `"ic-sender-info"`:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
``` [2](#0-1) 

So the full signed bytes are: `"\x0Eic-sender-info" || info_bytes` — nothing else.

The validator in `verify_sender_info_canister_sig` constructs the signable as:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
verify_canister_sig_with_fallback!(..., &canister_sig, &sender_info_content, ...);
``` [3](#0-2) 

The verification checks only that the canister signature is valid over the `info` bytes. It does **not** check:
- The message ID of the request
- The target canister ID
- The sender principal
- Any nonce or timestamp

By contrast, the envelope-level `sender_sig` is a canister signature over the `MessageId`, which does include the full request content (canister ID, method, args, ingress expiry, and the `sender_info` bytes themselves):

```rust
pub(crate) fn representation_independent_hash_call_or_query(
    ...
    ingress_expiry: u64,
    sender: &[u8],
    nonce: Option<&[u8]>,
    sender_info: Option<RawSignedSenderInfoSlices<'_>>,
) -> [u8; 32] { ... }
``` [4](#0-3) 

The `sender_info.sig` is a **separate, context-free** attestation. Once a signing canister (e.g., II) produces a canister signature over `info = [role=admin]` for a given seed/public key, that signature is valid for **any** future request from the same sender that embeds those same `info` bytes — regardless of which canister is called, what method is invoked, or whether the signing canister has since revoked the attested attributes.

---

### Impact Explanation

Canisters that read `msg_caller_info` to make access-control decisions (e.g., granting elevated privileges based on attested user attributes) can be bypassed by a user replaying a previously captured `sender_info` signature. Concretely:

1. II certifies `info = [role=admin]` for user U (seed S), producing `sig1`.
2. User U uses `{info, sig1}` to call canister A and is granted admin access.
3. II later revokes U's admin role (its certified state no longer contains `[role=admin]` for seed S).
4. User U constructs a new request R2 to canister A (or any other canister) embedding the old `{info, sig1}`.
5. User U obtains a valid `sender_sig` for R2 from II (II signs R2's message ID, which embeds the old `sender_info`; the IC protocol does not require II to validate the embedded `sender_info` before signing).
6. The replica accepts R2: `verify_sender_info_canister_sig` passes because `sig1` is still a valid canister signature over `info`, and canister A grants admin access based on the stale attested attributes.

The impact is an **ingress validation bypass**: revoked or stale identity attributes can be presented as currently valid, undermining the access-control guarantees that canisters build on top of `msg_caller_info`.

---

### Likelihood Explanation

The attack is reachable by an unprivileged ingress sender (the user themselves) without any privileged access. The only precondition is that the user has previously obtained a valid `sender_info` signature and that the signing canister does not validate the embedded `sender_info` when producing the `sender_sig`. Because the IC protocol places no such requirement on signing canisters, this is a realistic scenario. The attack is not theoretical: any canister that uses `msg_caller_info` for access control and relies on the signing canister to enforce revocation is affected.

---

### Recommendation

Bind `SenderInfoContent` to the specific request by including the `MessageId` (or at minimum the `ingress_expiry` and a nonce) in the signed bytes:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub message_id: &'a MessageId,  // binds to the specific request
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.info);
        bytes.extend_from_slice(self.message_id.as_bytes());
    }
}
```

This ensures that a `sender_info` signature is valid only for the specific request it was produced for, preventing replay across different requests or after revocation.

---

### Proof of Concept

1. User U holds `sender_pubkey = P` (canister sig public key: II canister + seed S).
2. II certifies `info = b"role=admin"` for seed S → `sig1 = canister_sig(SenderInfoContent(b"role=admin"))`.
3. User U sends R1 = `{canister_id: A, method: "admin_op", sender_info: {info: b"role=admin", signer: II, sig: sig1}, sender_sig: sig_R1}`. Canister A grants access.
4. II revokes: removes `b"role=admin"` from its certified state for seed S.
5. User U constructs R2 = `{canister_id: A, method: "admin_op", sender_info: {info: b"role=admin", signer: II, sig: sig1}, sender_sig: sig_R2}` where `sig_R2` is a fresh canister signature from II over R2's message ID (II signs the message ID without inspecting the embedded `sender_info`).
6. Replica calls `verify_sender_info_canister_sig`: verifies `sig1` over `SenderInfoContent(b"role=admin")` — **passes**, because `sig1` is still a structurally valid canister signature over those bytes.
7. Canister A receives R2 with `msg_caller_info = b"role=admin"` and grants admin access — **despite II having revoked the role**.

The root cause is at: [1](#0-0) [5](#0-4)

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

**File:** rs/types/types/src/messages/http.rs (L342-348)
```rust
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

**File:** rs/validator/src/ingress_validation.rs (L529-544)
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
    Ok(())
```
