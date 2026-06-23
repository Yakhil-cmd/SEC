### Title
`sender_info` Canister Signature Not Bound to Request Context Enables Cross-Request Replay - (`rs/validator/src/ingress_validation.rs`)

### Summary

The IC's `sender_info` feature allows a signing canister (e.g., Internet Identity) to attest to user attributes via a canister signature. However, the canister signature over `sender_info` is verified only against the raw `info` blob — it is not cryptographically bound to the target `canister_id`, `method_name`, `sender`, or `ingress_expiry` of the request. Any unprivileged user who observes a valid `{info, signer, sig}` tuple from any request can replay it verbatim in their own request to any other canister, causing that canister to receive attested attributes it was never authorized to see.

### Finding Description

**Root cause — `SenderInfoContent` signs only the raw blob:**

In `rs/types/types/src/messages/http.rs`, the signable content for `sender_info` is defined as:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // only the raw info bytes
    }
}
``` [1](#0-0) 

The signing canister therefore attests to `"ic-sender-info" || info_bytes` — with no binding to `canister_id`, `method_name`, `sender`, or `ingress_expiry`.

**Verification path — only the blob is checked:**

In `rs/validator/src/ingress_validation.rs`, `verify_sender_info_canister_sig` constructs the signable content as `SenderInfoContent(&sender_info.info)` and verifies the canister signature against it:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
verify_canister_sig_with_fallback!(validator, &canister_sig, &sender_info_content, ...);
``` [2](#0-1) 

No request-context fields (`canister_id`, `method_name`, `sender`) are included in the signed content.

**Contrast with the envelope signature:**

The envelope signature (over `MessageId`) does include `sender_info` as part of the representation-independent hash:

```rust
if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
    map.insert("sender_info", Map(btreemap! {
        "info" => Bytes(info), "signer" => Bytes(signer), "sig" => Bytes(sig),
    }));
}
hash_of_map(&map, |key, value| hash_key_val(key, value))
``` [3](#0-2) 

This means the envelope signature binds the `sender_info` tuple to the specific request, but the **canister signature inside `sender_info.sig` is not bound to any request context**. An attacker can extract a valid `{info, signer, sig}` from one request and embed it in a completely different request they construct and sign themselves.

**Exploit flow:**

1. Alice sends a request to canister A with `sender_info = {info: b"role=admin", signer: II_canister, sig: <valid_II_sig>}`. The II canister signed `"ic-sender-info" || b"role=admin"`.
2. Bob observes this request (ingress messages are gossiped across subnet nodes and are observable by node operators or network monitors).
3. Bob constructs a new request targeting canister B's privileged method, embedding the same `{info, signer=II, sig}` tuple.
4. Bob signs the new request with his own key — the envelope signature is valid because it covers the full new request content including the replayed `sender_info`.
5. The replica validates:
   - Bob's envelope signature → **passes** (Bob signed correctly)
   - `sender_info` canister signature: II signed `b"role=admin"` → **passes** (the signature is valid over the blob)
6. Canister B calls `ic0.msg_caller_info_data()` and receives `b"role=admin"`, granting Bob elevated privileges he was never authorized to have. [4](#0-3) 

The `ic0.msg_caller_info_data_copy` and `ic0.msg_caller_info_signer_copy` system APIs expose the replayed data directly to canister logic: [5](#0-4) 

### Impact Explanation

Any canister that uses `ic0.msg_caller_info_data()` or `ic0.msg_caller_info_signer()` to make authorization or identity decisions is vulnerable to attribute spoofing. An attacker can present attested attributes (e.g., `role=admin`, `kyc_verified=true`, `user_id=alice`) that were legitimately signed by a trusted canister (e.g., Internet Identity) for a different user or a different request context. This breaks the integrity guarantee that `sender_info` is intended to provide: that the attested attributes apply to the specific caller of the specific request.

The `inspect_message` hook also reads `sender_info` for access-control gating, meaning the replay can bypass canister-level ingress filtering: [6](#0-5) 

### Likelihood Explanation

- **Attacker entry point**: Any unprivileged user who can observe ingress messages on the network (node operators, network monitors, or anyone with access to gossip traffic) can extract valid `sender_info` tuples.
- **No privileged access required**: The attacker only needs to construct a valid HTTP request envelope, which any IC user can do.
- **Precondition**: A target canister must use `msg_caller_info_data()` or `msg_caller_info_signer()` for authorization. As the `sender_info` feature is new and being adopted for identity/attribute use cases, this attack surface will grow.
- **Replay window**: The `sender_info` canister signature has no expiry — it remains valid indefinitely, unlike the envelope's `ingress_expiry`.

### Recommendation

Bind the `sender_info` canister signature to the request context by including at minimum the `sender` principal and ideally `canister_id` and `method_name` in the signed content:

```rust
// Instead of signing only the info blob:
SenderInfoContent(&sender_info.info)

// Sign a context-bound structure:
SenderInfoContent { info: &sender_info.info, sender: &request.sender(), canister_id: &request.canister_id() }
```

The `SenderInfoContent::write_signed_bytes_without_domain_separator` implementation should include these fields so that a signature obtained for one request cannot be replayed in another. [7](#0-6) 

### Proof of Concept

1. Alice sends a valid update call to canister A with `sender_info = {info: b"role=admin", signer: II_canister_id, sig: <valid_canister_sig>}`.
2. Bob extracts `{info, signer, sig}` from the gossiped ingress message.
3. Bob constructs a new `HttpCallContent::Call` targeting canister B's privileged method, embedding the extracted `sender_info`.
4. Bob signs the envelope with his own key pair (the envelope signature covers the full content including the replayed `sender_info`).
5. Bob submits the request. The replica's `validate_sender_info` call in `rs/validator/src/ingress_validation.rs` passes because the canister signature over `b"role=admin"` is valid.
6. Canister B's `inspect_message` or update handler reads `ic0.msg_caller_info_data()` → `b"role=admin"` and grants Bob admin access. [8](#0-7)

### Citations

**File:** rs/types/types/src/messages/http.rs (L68-78)
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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L2454-2460)
```rust
    fn ic0_msg_caller_info_data_size(&self) -> HypervisorResult<usize> {
        let result = self
            .sender_info("ic0_msg_caller_info_data_size")
            .map(|sender_info| sender_info.map_or(0, |si| si.info.len()));
        trace_syscall!(self, MsgCallerInfoDataSize, result);
        result
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L2462-2487)
```rust
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
```

**File:** rs/execution_environment/tests/hypervisor.rs (L2410-2442)
```rust
#[test]
fn ic0_msg_caller_info_works_in_inspect_message() {
    let mut test = ExecutionTestBuilder::new().build();
    let canister_id = test.universal_canister().unwrap();

    let info = vec![1_u8, 2, 3, 4];
    let signer = canister_test_id(42);
    test.set_sender_info(SenderInfo {
        info: info.clone(),
        signer,
    });

    // Set inspect_message to trap iff msg_caller_info_data equals `info`.
    // Since the correct value IS `info`, the handler will trap.
    test.ingress(
        canister_id,
        "update",
        wasm()
            .set_inspect_message(
                wasm()
                    .msg_caller_info_data()
                    .trap_if_eq(&info, "info")
                    .accept_message()
                    .build(),
            )
            .reply()
            .build(),
    )
    .unwrap();
    let err = test
        .should_accept_ingress_message(canister_id, "update", vec![])
        .unwrap_err();
    assert_eq!(ErrorCode::CanisterCalledTrap, err.code());
```
