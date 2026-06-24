### Title
`SenderInfoContent` Signature Lacks Request-Context Binding, Enabling Cross-Canister Replay — (File: `rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `sender_info` canister signature is computed over only the raw `info` blob with a fixed domain separator (`"ic-sender-info"`). It does not bind to the target canister ID, the request `MessageId`, or any other request-specific context. A valid `sender_info` signature observed from one ingress request can be replayed verbatim in a different request targeting a different canister, causing that canister to receive attested user attributes it was never meant to receive.

---

### Finding Description

The `SenderInfoContent` type defines the signed bytes as:

```
\x0Eic-sender-info || info_blob
``` [1](#0-0) 

The `SignatureDomain` implementation confirms the domain is the static string `"ic-sender-info"` with no per-request fields: [2](#0-1) 

In `verify_sender_info_canister_sig`, the validator:
1. Checks that `sender_pubkey` encodes a valid canister signature public key.
2. Checks that the canister ID in `sender_pubkey` matches `sender_info.signer`.
3. Verifies the canister signature over `SenderInfoContent(&sender_info.info)` — the raw `info` blob only. [3](#0-2) 

Critically, the signed content does **not** include:
- The target canister ID (`canister_id` of the request)
- The request `MessageId`
- The sender principal
- Any other request-specific field

The `MessageId` does include the full `sender_info` struct (including `sig`) in its hash: [4](#0-3) 

But this only prevents the *outer* ingress signature from being replayed — it does not prevent the `sender_info.sig` itself from being extracted and reused in a freshly constructed request with a different `MessageId`, different target canister, or different method/args.

---

### Impact Explanation

The `sender_info` feature is designed to let a canister (e.g., Internet Identity) attest user attributes to a target canister via `ic0.msg_caller_info_data_copy` / `ic0.msg_caller_info_signer_copy`: [5](#0-4) 

A target canister that uses `msg_caller_info_data` for access control (e.g., "user has role=admin", "user has passed KYC") will receive the `info` blob and `signer` as if they were freshly attested for that specific request. Because the signature does not bind to the target canister ID or request context, an attacker who observes a valid `sender_info` from any prior request can replay it against any other canister that trusts the same signer. The replaying canister receives attested attributes it was never meant to receive, potentially granting unauthorized access.

---

### Likelihood Explanation

IC ingress messages are submitted over the public HTTP API and are observable by any boundary node or network participant. An attacker can extract the `sender_info.sig` and `sender_info.info` from any observed request and construct a new request to a different target canister with the same `sender_info`. No privileged access is required. The `sender_info` feature is new and canisters adopting it for access control are unlikely to be aware of this replay risk since the protocol does not prevent it. Likelihood is medium — it requires observing a valid `sender_info` in the wild, which becomes more probable as adoption grows.

---

### Recommendation

Bind the `SenderInfoContent` signed bytes to at minimum the target canister ID, and ideally also the request `MessageId`. The signed content should be:

```
\x0Eic-sender-info || target_canister_id || info_blob
```

or equivalently include the `MessageId` for full per-request binding. This mirrors the ERC-7739 defensive rehashing approach: the signed hash must encode the context in which it is valid, preventing cross-context replay.

The `SenderInfoContent` struct and its `write_signed_bytes_without_domain_separator` implementation in `rs/types/types/src/messages/http.rs` must be updated to accept and include the target canister ID. The `verify_sender_info_canister_sig` function in `rs/validator/src/ingress_validation.rs` must pass the target canister ID when constructing the `SenderInfoContent` for verification.

---

### Proof of Concept

**Setup:**
- Internet Identity canister `II` signs `info = b"role=admin"` for user `U`.
- User `U` sends a valid request to canister `A` with `sender_info = {info: b"role=admin", signer: II, sig: σ}`.

**Attack:**
1. Attacker observes the request and extracts `{info: b"role=admin", signer: II, sig: σ}`.
2. Attacker constructs a new request to canister `B` (an admin-gated canister) with:
   - `sender = U` (or any principal that derives from the same `sender_pubkey`)
   - `sender_info = {info: b"role=admin", signer: II, sig: σ}` (identical, replayed)
3. The replica calls `verify_sender_info_canister_sig`:
   - `pubkey_canister_id == sender_info.signer` ✓ (II == II)
   - Canister signature over `\x0Eic-sender-info || b"role=admin"` verifies ✓ (same bytes, same sig)
4. Validation passes. Canister `B` receives `msg_caller_info_data = b"role=admin"` and `msg_caller_info_signer = II`.
5. Canister `B` grants admin access to the attacker.

The signed bytes are identical in both requests because `SenderInfoContent` does not include the target canister ID or `MessageId`. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/validator/src/ingress_validation.rs (L513-521)
```rust
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

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L367-393)
```rust
    linker
        .func_wrap("ic0", "msg_caller_info_data_size", {
            move |mut caller: Caller<'_, StoreData>| {
                charge_for_cpu(&mut caller, overhead::MSG_CALLER_INFO_DATA_SIZE)?;
                with_system_api(&mut caller, |s| s.ic0_msg_caller_info_data_size()).and_then(|s| {
                    I::try_from(s).map_err(|e| {
                        wasmtime::Error::msg(format!("ic0::msg_caller_info_data_size failed: {e}"))
                    })
                })
            }
        })
        .unwrap();

    linker
        .func_wrap("ic0", "msg_caller_info_signer_copy", {
            move |mut caller: Caller<'_, StoreData>, dst: I, offset: I, size: I| {
                let dst: usize = dst.try_into().expect("Failed to convert I to usize");
                let offset: usize = offset.try_into().expect("Failed to convert I to usize");
                let size: usize = size.try_into().expect("Failed to convert I to usize");
                charge_for_cpu_and_mem(&mut caller, overhead::MSG_CALLER_INFO_SIGNER_COPY, size)?;
                with_memory_and_system_api(&mut caller, |system_api, memory| {
                    system_api.ic0_msg_caller_info_signer_copy(dst, offset, size, memory)
                })?;
                Ok(())
            }
        })
        .unwrap();
```
