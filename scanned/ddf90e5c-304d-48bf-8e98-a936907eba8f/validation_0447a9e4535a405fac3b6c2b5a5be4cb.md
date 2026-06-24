### Title
`SenderInfoContent` Canister Signature Does Not Bind to Request Context, Enabling Cross-Request Replay - (File: `rs/types/types/src/messages/http.rs`)

---

### Summary

The `sender_info` canister signature verified in `verify_sender_info_canister_sig` covers only the raw `info` blob under the domain separator `"ic-sender-info"`. It does not include the target `canister_id`, `sender` principal, `MessageId`, `method_name`, or `ingress_expiry`. A user who has obtained a valid `sender_info` attestation from a signing canister (e.g., Internet Identity) for one request can replay the identical `(info, signer, sig)` triple in any subsequent request to any other canister, causing that canister to observe the replayed attestation via `ic0_msg_caller_info_data_copy` as if the signing canister had freshly attested those attributes for that specific request.

---

### Finding Description

`SenderInfoContent` is defined in `rs/types/types/src/messages/http.rs` as a thin wrapper over the raw `info` bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // only the info blob
    }
}
``` [1](#0-0) 

Its `SignatureDomain` implementation in `rs/types/types/src/crypto/sign.rs` uses only the static string `"ic-sender-info"`:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
``` [2](#0-1) 

The bytes actually signed are therefore: `\x0Eic-sender-info` ‖ `info_bytes` — with no inclusion of `canister_id`, `sender`, `MessageId`, `method_name`, or `ingress_expiry`.

In `verify_sender_info_canister_sig` (`rs/validator/src/ingress_validation.rs`), the replica verifies:
1. `sender_pubkey` is a valid canister signature public key.
2. The canister ID embedded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid. [3](#0-2) 

There is no step that checks whether the `sender_info.sig` was produced for this specific request. The `(info, signer, sig)` triple is fully portable across any request made by the same sender principal.

The outer envelope signature (over the `MessageId`) does cover the `sender_info` fields because `representation_independent_hash_call_or_query` includes them: [4](#0-3) 

However, this only prevents a third party from injecting a `sender_info` into someone else's request. It does not prevent the legitimate sender from reusing their own previously-obtained `sender_info` signature in a different request to a different canister.

---

### Impact Explanation

Any canister that uses `ic0_msg_caller_info_data_copy` / `ic0_msg_caller_info_signer_copy` for access-control decisions is vulnerable. The signing canister (e.g., Internet Identity) cannot scope its attestation to a specific target canister or request context, because the signed payload contains no such binding. A user who legitimately obtained a `sender_info` attestation of `b"role=admin"` for canister X can replay the same `(info, signer, sig)` in a request to canister Y. Canister Y's `inspect_message` or update handler will observe `msg_caller_info_data` = `b"role=admin"` as if the signing canister had freshly attested those attributes for that specific call. [5](#0-4) 

---

### Likelihood Explanation

Medium. The `sender_info` feature is newly introduced and its primary use case is for identity providers (like Internet Identity) to attest caller attributes. Canister developers who use `msg_caller_info_data` for access control without additional context binding (e.g., without encoding the target canister ID inside the `info` blob itself) are directly exploitable. The attacker only needs to have made one legitimate request through the signing canister to obtain a reusable attestation. No privileged access, key compromise, or subnet-majority corruption is required.

---

### Recommendation

The signed content for `sender_info` must include request-binding context. The `SenderInfoContent` signable should incorporate at minimum the target `canister_id` and the `sender` principal (or the full `MessageId`) so that a signature produced for one request cannot be replayed against another. Concretely, `write_signed_bytes_without_domain_separator` should serialize `canister_id ‖ sender ‖ info_bytes` (or `message_id ‖ info_bytes`) rather than `info_bytes` alone. The signing canister and the replica validator must agree on this extended format. [6](#0-5) 

---

### Proof of Concept

**Setup:**
- Signing canister C with seed S; user principal P = `self_authenticating(DER(C, S))`.
- Canister X and canister Y both deployed on the IC.
- Canister Y's `inspect_message` grants access only if `msg_caller_info_data` == `b"role=admin"`.

**Steps:**

1. User P makes a legitimate request to canister X with `sender_info`:
   ```
   info   = b"role=admin"
   signer = C
   sig    = canister_sig_C_S( \x0Eic-sender-info || b"role=admin" )
   ```
   The signing canister C certifies this in its certified data tree. The replica accepts it.

2. User P constructs a new request to canister Y with **the identical** `sender_info = {info, signer, sig}` but different `canister_id`, `method_name`, and `arg`. P signs the outer envelope with their own key (valid, since P controls the private key for seed S).

3. The replica calls `verify_sender_info_canister_sig`:
   - Checks `sender_pubkey` encodes canister C ✓
   - Checks `sender_info.signer == C` ✓
   - Verifies `sig` over `SenderInfoContent(b"role=admin")` ✓ (same bytes, same certificate)
   - **No check that this sig was produced for canister Y or this MessageId.**

4. The replica accepts the request. Canister Y's `inspect_message` reads `msg_caller_info_data` = `b"role=admin"` and grants access — even though signing canister C never attested those attributes for canister Y. [7](#0-6) [2](#0-1)

### Citations

**File:** rs/types/types/src/messages/http.rs (L43-78)
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

**File:** rs/validator/src/ingress_validation.rs (L490-545)
```rust
/// Verifies that the sender_info canister signature is valid:
/// 1. The envelope-level sender_pubkey is a valid canister signature public key.
/// 2. The canister ID encoded in sender_pubkey matches the signer field.
/// 3. The signature over the info blob is valid against the root of trust.
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
}
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L2454-2498)
```rust
    fn ic0_msg_caller_info_data_size(&self) -> HypervisorResult<usize> {
        let result = self
            .sender_info("ic0_msg_caller_info_data_size")
            .map(|sender_info| sender_info.map_or(0, |si| si.info.len()));
        trace_syscall!(self, MsgCallerInfoDataSize, result);
        result
    }

    fn ic0_msg_caller_info_data_copy(
        &self,
        dst: usize,
        offset: usize,
        size: usize,
        heap: &mut [u8],
    ) -> HypervisorResult<()> {
        let result = self
            .sender_info("ic0_msg_caller_info_data_copy")
            .and_then(|sender_info| {
                let info_bytes: &[u8] = sender_info.map_or(&[], |si| si.info.as_slice());
                valid_subslice(
                    "ic0.msg_caller_info_data_copy heap",
                    InternalAddress::new(dst),
                    InternalAddress::new(size),
                    heap,
                )?;
                let slice = valid_subslice(
                    "ic0.msg_caller_info_data_copy info",
                    InternalAddress::new(offset),
                    InternalAddress::new(size),
                    info_bytes,
                )?;
                deterministic_copy_from_slice(&mut heap[dst..dst + size], slice);
                Ok(())
            });
        trace_syscall!(
            self,
            MsgCallerInfoDataCopy,
            result,
            dst,
            offset,
            size,
            summarize(heap, dst, size)
        );
        result
    }
```
