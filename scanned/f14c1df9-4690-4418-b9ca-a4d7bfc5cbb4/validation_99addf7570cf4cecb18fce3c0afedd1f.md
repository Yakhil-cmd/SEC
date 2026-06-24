### Title
`SenderInfoContent` Signed Payload Lacks Sender-Principal Binding and Expiry — Enables `sender_info` Replay Across Requests - (File: `rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The IC's `sender_info` mechanism allows a signing canister (e.g., Internet Identity) to attest user attributes to a target canister via `msg_caller_info_data()`. The signed payload (`SenderInfoContent`) contains only the raw `info` bytes — it does not include the sender's principal (`UserId`) or any expiry timestamp. The protocol-level verification (`verify_sender_info_canister_sig`) does not enforce that the `info` blob is bound to the specific sender. This allows a user to replay a previously obtained `sender_info` signature across multiple fresh ingress messages, and allows a signing canister to issue the same `info` blob for multiple users without protocol-level user binding.

---

### Finding Description

`SenderInfoContent` is the signable type used to verify the `sender_info` field in IC ingress messages:

```rust
// rs/types/types/src/messages/http.rs:341-348
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);  // only the raw info blob
    }
}
``` [1](#0-0) 

The signed bytes are: `domain_separator("ic-sender-info") || info_blob`. The sender's principal (`UserId`) and any expiry timestamp are absent from the signed content.

The verification function `verify_sender_info_canister_sig` checks three things:

1. `sender_pubkey` is a valid canister signature public key
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid [2](#0-1) 

Critically, step 3 verifies the signature over only the `info` blob — the sender's principal is never included in the signed content. The `validate_sender_info` caller does have access to `request.sender()`, but it is never passed into the signed-content construction: [3](#0-2) 

The `SignedSenderInfo` struct carries `info`, `signer`, and `sig` — no expiry field: [4](#0-3) 

The `SenderInfo` struct (post-validation, passed to the canister via `msg_caller_info_data_copy`) also carries only `info` and `signer`: [5](#0-4) 

---

### Impact Explanation

Any canister that uses `ic0.msg_caller_info_data_copy` / `ic0.msg_caller_info_signer_copy` to make access-control decisions (e.g., "user has premium role", "user is KYC-verified") is exposed to the following attack:

1. User A legitimately obtains a valid `sender_info` from a signing canister (e.g., Internet Identity) attesting `info = {role: "premium"}`.
2. User A's premium access is revoked. The signing canister removes the certified variable.
3. During the window before the canister's BLS certificate expires (typically a few minutes), User A can attach the old `(sender_pubkey, sig, info)` tuple to any new ingress message with a fresh `ingress_expiry`.
4. The protocol accepts the `sender_info` because the canister signature certificate is still valid.
5. The target canister reads `msg_caller_info_data()` and grants premium access to a revoked user.

Additionally, if the signing canister issues a generic `info` blob (not user-specific, e.g., `info = {role: "premium"}`) and uses a shared certified variable, any user who obtains the signature can present it — the protocol does not enforce that the `info` blob contains the sender's principal.

The system APIs that expose this data to canisters are production-deployed: [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The `sender_info` feature is production-deployed and designed for Internet Identity to pass user attributes to canisters. Any canister that uses `msg_caller_info_data()` for access control without independently verifying the user's current status is vulnerable. The replay window is bounded by the canister signature certificate validity period (minutes), but this is sufficient for an attacker who monitors revocation events. The attack requires no privileged access — only a previously valid `sender_info` signature, which any legitimate user of the signing canister can obtain.

---

### Recommendation

1. **Include the sender's principal in `SenderInfoContent`**: The signed bytes should be `domain_separator || sender_principal_bytes || info_blob`, so the signature is cryptographically bound to the specific user. This prevents cross-user replay and forces the signing canister to issue user-specific signatures.

2. **Add an expiry field to `SenderInfoContent`**: Include a timestamp or expiry in the signed content and enforce it in `verify_sender_info_canister_sig`, analogous to how `validate_sender_delegation_expiry` enforces delegation expiry: [7](#0-6) 

3. **Document the replay risk**: Until the above changes are made, document that signing canisters must include the sender's principal and an expiry in the `info` blob, and that target canisters must not rely solely on `msg_caller_info_data()` for access control without independent verification.

---

### Proof of Concept

**Step 1:** Signing canister (e.g., II) issues `sender_info` for user A:
```
info = b"role=premium"
sig = canister_sign(SenderInfoContent(info))  // signed bytes = "ic-sender-info" || info
                                               // sender principal NOT included
```

**Step 2:** User A's premium access is revoked. II removes the certified variable.

**Step 3:** During the certificate validity window, User A constructs a new ingress message:
```
HttpCanisterUpdate {
    sender: user_A_principal,
    ingress_expiry: now + 5_minutes,  // fresh expiry
    sender_info: Some(RawSignedSenderInfo {
        info: b"role=premium",        // old info blob
        signer: II_canister_id,
        sig: old_sig,                 // old signature, still valid during cert window
    }),
    ...
}
```

**Step 4:** `verify_sender_info_canister_sig` passes — the canister signature certificate is still valid.

**Step 5:** Target canister executes:
```wasm
(call $ic0.msg_caller_info_data_copy ...)
;; reads b"role=premium" — grants premium access to revoked user
```

The `SenderInfoContent` construction at `rs/types/types/src/messages/http.rs:344-347` confirms the sender principal is never included in the signed bytes, and `verify_sender_info_canister_sig` at `rs/validator/src/ingress_validation.rs:529-531` confirms the verification is only over the `info` blob. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/types/types/src/messages/http.rs (L328-334)
```rust
/// Signed sender info after decoding.
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct SignedSenderInfo {
    pub info: Vec<u8>,
    pub signer: CanisterId,
    pub sig: Vec<u8>,
}
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

**File:** rs/validator/src/ingress_validation.rs (L494-545)
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
}
```

**File:** rs/validator/src/ingress_validation.rs (L606-622)
```rust
fn validate_sender_delegation_expiry(
    sender_delegation: &Option<Vec<SignedDelegation>>,
    current_time: Time,
) -> Result<(), RequestValidationError> {
    if let Some(delegations) = &sender_delegation {
        for delegation in delegations.iter() {
            let expiry = delegation.delegation().expiration();
            if delegation.delegation().expiration() < current_time {
                return Err(InvalidDelegationExpiry(format!(
                    "Specified sender delegation has expired:\n\
                     Provided expiry:    {expiry}\n\
                     Local replica time: {current_time}",
                )));
            }
        }
    }
    Ok(())
```

**File:** rs/types/types/src/messages/ingress_messages.rs (L395-401)
```rust
/// Sender info after decoding and signature validation.
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct SenderInfo {
    #[serde(with = "serde_bytes")]
    pub info: Vec<u8>,
    pub signer: CanisterId,
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
