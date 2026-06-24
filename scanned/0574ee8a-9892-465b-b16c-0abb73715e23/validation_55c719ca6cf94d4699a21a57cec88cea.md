### Title
`SenderInfoContent` Signature Missing Target Canister ID Enables Cross-Canister Replay - (File: `rs/validator/src/ingress_validation.rs`)

### Summary

The `sender_info` ingress feature allows a signing canister (e.g., Internet Identity) to attest to an opaque `info` blob about the caller. The signed message is constructed as `domain("ic-sender-info") || info_bytes` with no target canister ID bound into the signature. An unprivileged ingress sender who obtains a valid `sender_info` signature for canister A can replay the identical `{info, signer, sig}` triple in requests to any other canister B, causing B to observe the same attested `info` blob as if the signing canister had issued it specifically for B.

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over the raw `info` bytes: [1](#0-0) 

Its `SignatureDomain` implementation prepends only the fixed string `"ic-sender-info"`: [2](#0-1) 

So the bytes that the signing canister actually signs are:

```
\x0Eic-sender-info || info_bytes
```

No target canister ID, no request-specific nonce, no expiry — nothing that binds the signature to a particular destination canister.

The boundary-node validator verifies the signature in `verify_sender_info_canister_sig`: [3](#0-2) 

The only checks performed are:
1. `sender_pubkey` is a valid canister-signature public key.
2. The canister ID encoded in `sender_pubkey` matches the declared `signer` field.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is cryptographically valid.

There is no check that the `info` blob contains the target canister ID, nor any other binding to the destination canister. The `validate_sender_info` wrapper that calls this function also performs no such check: [4](#0-3) 

Once the signature passes, the `info` blob is forwarded verbatim to the target canister via the `ic0_msg_caller_info_data_copy` / `ic0_msg_caller_info_signer_copy` system APIs: [5](#0-4) 

### Impact Explanation

Any canister that uses `ic0_msg_caller_info_data_copy` to make authorization decisions based on the attested `info` blob (e.g., "if the signing canister attests attribute X, grant privilege Y") is vulnerable to cross-canister replay. An attacker who legitimately obtains a `sender_info` signature from Internet Identity (or any other signing canister) for canister A can submit the identical `{info, signer, sig}` triple in requests to canister B. Canister B will observe the same attested blob as if the signing canister had issued it specifically for B, potentially bypassing access controls, privilege checks, or identity-gating logic that the canister developer assumed were canister-specific.

This is a structural protocol-level issue: the signed payload is canister-agnostic by construction, so every canister that trusts `sender_info` for authorization is affected, not just a single misconfigured canister.

### Likelihood Explanation

The `sender_info` feature is explicitly designed for use by identity providers such as Internet Identity. Canisters that integrate with Internet Identity and use `sender_info` for authorization are the primary target. An attacker only needs to:
1. Legitimately trigger a `sender_info` signature from the signing canister for one target canister (a normal user flow).
2. Replay the same `{info, signer, sig}` triple in requests to a second canister.

No privileged access, no key compromise, and no threshold corruption is required. The attacker-controlled entry path is a standard ingress HTTP call with a crafted envelope.

### Recommendation

Bind the signed payload to the target canister ID. The `SenderInfoContent` signed bytes should include the destination canister ID, for example:

```
\x0Eic-sender-info || canister_id_bytes || info_bytes
```

Alternatively, include the target canister ID as a mandatory field inside the `info` blob and have `verify_sender_info_canister_sig` extract and verify it against the request's `canister_id`. Either approach prevents a signature issued for canister A from being accepted by canister B.

### Proof of Concept

1. User U authenticates with Internet Identity (II) and obtains a `sender_info` for canister A:
   - II signs `\x0Eic-sender-info || b"user-is-premium"` with its canister signature key.
   - The resulting `{info: b"user-is-premium", signer: II_canister_id, sig: <sig>}` is embedded in an ingress call to canister A.
   - Canister A reads `ic0_msg_caller_info_data_copy` → `b"user-is-premium"` and grants premium access.

2. U (or an attacker who observed the envelope) constructs an identical ingress call to canister B, reusing the same `{info, signer, sig}` triple verbatim.

3. The boundary node runs `verify_sender_info_canister_sig`:
   - Checks `sender_pubkey` → valid canister sig key for II. ✓
   - Checks `signer` field matches II canister ID. ✓
   - Verifies canister signature over `SenderInfoContent(b"user-is-premium")`. ✓ (same bytes, same signature)
   - **No check on target canister ID.** ✓ (passes)

4. Canister B receives the call with `ic0_msg_caller_info_data_copy` → `b"user-is-premium"` and grants premium access — even though II never issued this attestation for canister B. [6](#0-5) [7](#0-6)

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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L2462-2498)
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
