### Title
`SenderInfoContent` Digest Does Not Bind to the Request Sender — Cross-User Replay of Canister-Signed Attestations - (File: `rs/types/types/src/messages/http.rs`)

---

### Summary

The `SenderInfoContent` signed digest contains only the raw `info` bytes and a domain separator. The `sender` (user principal), `canister_id`, and `ingress_expiry` are absent from what the attesting canister signs. Any user who obtains a valid `(info, signer, sig)` triple — e.g., by observing another user's HTTP request — can attach it verbatim to their own ingress message. The replica's `verify_sender_info_canister_sig` will accept it because all three of its checks pass: the outer envelope signature is valid (the attacker signed it with their own key), the inner canister signature is valid (the attesting canister did sign those `info` bytes), and the signer field matches the canister ID in the public key.

---

### Finding Description

The `sender_info` field in IC ingress messages allows a canister (e.g., Internet Identity) to attach a signed attestation blob to a request. The attesting canister produces a canister signature over a `SenderInfoContent` value.

`SenderInfoContent` is defined as a newtype over the raw `info` bytes: [1](#0-0) 

Its `write_signed_bytes_without_domain_separator` implementation appends only those raw bytes: [2](#0-1) 

The domain separator is `"ic-sender-info"`: [3](#0-2) 

So the full signed payload is:

```
\x0Eic-sender-info || info_bytes
```

The `sender` (user principal), `canister_id`, and `ingress_expiry` are **not** included. These fields are present in the outer message-ID hash: [4](#0-3) 

but the outer hash is signed by the **user**, not by the attesting canister. The attesting canister's signature covers only `info_bytes`.

The replica's validation in `verify_sender_info_canister_sig` performs three checks:

1. `sender_pubkey` is a valid canister-signature public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid. [5](#0-4) 

None of these checks bind the attestation to the specific sender principal or target canister. The `sender` field of the ingress message is never compared against anything inside the `info` bytes at the protocol level.

---

### Impact Explanation

An attacker who obtains a valid `(info, signer, sig)` triple — for example by observing another user's HTTP request, or by having the attesting canister sign a blob that encodes victim-specific attributes — can replay it in their own ingress message. The attacker:

1. Constructs an ingress message with their own `sender` principal.
2. Copies the victim's `(info, signer, sig)` into the `sender_info` field verbatim.
3. Signs the outer envelope with their own key (valid, because the message-ID hash includes the `sender_info` triple as opaque bytes).
4. Submits the request.

The replica accepts it. The target canister receives the request with `sender = attacker` but `sender_info` attesting to attributes that were signed for the victim. If the canister uses `msg_caller_info_data()` to make authorization decisions (e.g., "this session has admin role", "this user passed KYC"), the attacker inherits those privileges without the attesting canister ever having approved them for the attacker's principal.

---

### Likelihood Explanation

Medium. The attacker must obtain a valid `(info, signer, sig)` triple. Possible acquisition paths include:

- Observing another user's plaintext HTTP request to a boundary node (e.g., on a shared network, or via a compromised boundary node).
- Social engineering a victim into sharing their request envelope.
- Any application where the attesting canister signs the same `info_bytes` for multiple users (e.g., a shared session token or a role blob that is not user-specific).

The replay requires no privileged access, no threshold corruption, and no governance action. It is a pure ingress-level attack executable by any unprivileged sender.

---

### Recommendation

Include the `sender` principal (and optionally `canister_id` and `ingress_expiry`) in the `SenderInfoContent` signed digest. For example:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub sender: &'a [u8],       // user principal bytes
    pub canister_id: &'a [u8],  // target canister bytes
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // representation-independent hash of {info, sender, canister_id}
        ...
    }
}
```

This mirrors how the IC's `Delegation` struct binds `pubkey`, `expiration`, and optionally `targets` into its signed bytes, preventing cross-context reuse. [6](#0-5) 

---

### Proof of Concept

**Setup:**
- Attesting canister `C` (e.g., Internet Identity) signs `info_bytes = b"role=admin"` for user A's session. The resulting `sig_A` is a valid canister signature over `\x0Eic-sender-info || b"role=admin"`.
- User A submits a request with `sender_info = {info: b"role=admin", signer: C, sig: sig_A}`.

**Attack:**
1. Attacker (user B) intercepts or otherwise obtains `(info=b"role=admin", signer=C, sig=sig_A)`.
2. Attacker constructs:
   ```
   HttpCanisterUpdate {
       sender: B_principal,
       canister_id: target,
       sender_info: Some(RawSignedSenderInfo {
           info: b"role=admin",
           signer: C,
           sig: sig_A,   // originally produced for user A
       }),
       ...
   }
   ```
3. Attacker signs the outer envelope with their own key (valid).
4. Replica validates:
   - Outer signature: ✓ (B signed the message ID)
   - `sender_pubkey` is a canister sig key for C: ✓
   - Canister ID in pubkey == `signer` field: ✓
   - Canister sig on `SenderInfoContent(b"role=admin")`: ✓ (C did sign this)
5. Target canister receives `caller = B`, `msg_caller_info_data() = b"role=admin"` → grants admin access to B.

The root cause is in `SenderInfoContent::write_signed_bytes_without_domain_separator` at: [2](#0-1) 

which omits the `sender` from the attesting canister's signed digest, directly analogous to SFA-06's omission of the COLLATERAL token from the market-maker's signed digest.

### Citations

**File:** rs/types/types/src/messages/http.rs (L54-78)
```rust
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
```

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
