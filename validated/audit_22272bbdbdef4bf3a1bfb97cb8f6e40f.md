Audit Report

## Title
`SenderInfoContent` Signed Payload Lacks Target Canister ID Binding, Enabling Cross-Canister Replay - (File: `rs/validator/src/ingress_validation.rs`)

## Summary

The `sender_info` ingress feature allows a signing canister (e.g., Internet Identity) to attest an opaque `info` blob about the caller. The signed bytes are constructed as `domain("ic-sender-info") || info_bytes` with no target canister ID included. A valid `{info, signer, sig}` triple obtained for canister A can be replayed verbatim to any canister B, which will receive the same attested blob as if the signing canister had issued it specifically for B. This is a structural protocol-level flaw: the signed payload is canister-agnostic by construction.

## Finding Description

`SenderInfoContent` wraps only the raw `info` bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
}
``` [1](#0-0) 

Its `SignatureDomain` prepends only the fixed string `"ic-sender-info"`: [2](#0-1) 

So the bytes actually signed are `\x0Eic-sender-info || info_bytes` — no target canister ID, no request-specific nonce, no expiry.

`verify_sender_info_canister_sig` performs three checks: (1) valid canister sig public key, (2) signer field matches canister ID in pubkey, (3) signature over `SenderInfoContent(&sender_info.info)` is valid. There is no check that the `info` blob contains the target canister ID, nor any binding to the destination canister: [3](#0-2) 

`validate_sender_info` calls `verify_sender_info_canister_sig` without passing the request's canister ID: [4](#0-3) 

It is called from `validate_request_content` which has access to the full `request` (including `canister_id`) but does not thread it through: [5](#0-4) 

Once the signature passes, `ic0_msg_caller_info_data_copy` forwards the `info` blob verbatim to the executing canister: [6](#0-5) 

## Impact Explanation

Any canister that calls `ic0_msg_caller_info_data_copy` and uses the attested `info` blob for authorization (e.g., "if the signing canister attests attribute X, grant privilege Y") is vulnerable to cross-canister replay. An attacker who legitimately obtains a `sender_info` signature from Internet Identity for canister A can submit the identical `{info, signer, sig}` triple in requests to canister B. Canister B will observe the same attested blob as if the signing canister had issued it specifically for B, bypassing access controls or identity-gating logic. This matches the **High** impact category: significant Internet Identity and infrastructure security impact with concrete user or protocol harm.

## Likelihood Explanation

No special privileges, key compromise, or threshold corruption is required. The attacker only needs to: (1) legitimately trigger a `sender_info` signature from the signing canister for one target canister (a normal user flow), then (2) replay the same `{info, signer, sig}` triple in requests to a second canister. The attack is repeatable, low-cost, and requires only a standard ingress HTTP call with a crafted envelope.

## Recommendation

Bind the signed payload to the target canister ID. Modify `SenderInfoContent` to include the destination canister ID in the signed bytes:

```rust
pub struct SenderInfoContent<'a>(pub CanisterId, pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0.get_ref().as_slice());
        bytes.extend_from_slice(self.1);
    }
}
```

Pass the request's `canister_id` into `validate_sender_info` and `verify_sender_info_canister_sig`, and construct `SenderInfoContent(canister_id, &sender_info.info)` at the verification site. Alternatively, mandate that the `info` blob contain the target canister ID as a structured field and have `verify_sender_info_canister_sig` extract and verify it against the request's `canister_id`.

## Proof of Concept

1. User U authenticates with Internet Identity (II) and obtains a `sender_info` for canister A:
   - II signs `\x0Eic-sender-info || b"user-is-premium"` with its canister signature key.
   - The resulting `{info: b"user-is-premium", signer: II_canister_id, sig: <sig>}` is embedded in an ingress call to canister A.
   - Canister A reads `ic0_msg_caller_info_data_copy` → `b"user-is-premium"` and grants premium access.

2. U (or an observer of the envelope) constructs an identical ingress call to canister B, reusing the same `{info, signer, sig}` triple verbatim.

3. The boundary node runs `verify_sender_info_canister_sig`:
   - Checks `sender_pubkey` → valid canister sig key for II. ✓
   - Checks `signer` field matches II canister ID. ✓
   - Verifies canister signature over `SenderInfoContent(b"user-is-premium")`. ✓ (same bytes, same signature)
   - **No check on target canister ID.** → passes.

4. Canister B receives the call with `ic0_msg_caller_info_data_copy` → `b"user-is-premium"` and grants premium access — even though II never issued this attestation for canister B.

A deterministic integration test using `PocketIC` or `StateMachine` can reproduce this by: creating two canisters, obtaining a valid `sender_info` canister signature for canister A, submitting an otherwise-identical ingress message to canister B with the same `sender_info` fields, and asserting that `ic0_msg_caller_info_data_copy` returns the same attested blob in canister B.

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

**File:** rs/validator/src/ingress_validation.rs (L219-220)
```rust
    validate_sender_info(request, ingress_signature_verifier, root_of_trust_provider)?;
    Ok(targets)
```

**File:** rs/validator/src/ingress_validation.rs (L482-488)
```rust
    verify_sender_info_canister_sig(
        sender_info,
        sender_pubkey,
        ingress_signature_verifier,
        root_of_trust_provider,
    )
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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L2469-2487)
```rust
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
