Audit Report

## Title
`SenderInfoContent` Signature Not Bound to Request — Cross-Request Replay of Canister-Attested `sender_info` - (File: `rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

## Summary

`SenderInfoContent` serializes only the raw `info` bytes under a static `"ic-sender-info"` domain separator, with no binding to the request's `message_id`, `canister_id`, `method_name`, or `ingress_expiry`. The validator (`verify_sender_info_canister_sig`) enforces only that the canister signature is cryptographically valid over those bytes; it never checks request context. A valid `sender_info` obtained for any request can therefore be replayed verbatim in any other request signed with the same canister-signature key, bypassing authorization decisions made by canisters via `ic0_msg_caller_info_data_copy` / `ic0_msg_caller_info_signer_copy`.

## Finding Description

**Root cause — `SenderInfoContent` signs only the opaque `info` blob:**

`SenderInfoContent` is a newtype over a raw byte slice:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // only the info blob
    }
}
``` [1](#0-0) 

The domain separator is a static string with no request-specific component:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
``` [2](#0-1) 

The full signed bytes are therefore `\x0Eic-sender-info ‖ info_bytes` — with no `message_id`, no `canister_id`, no `method_name`, no `ingress_expiry`, and no subnet identifier.

**Verification path — `verify_sender_info_canister_sig` enforces no request binding:**

`validate_request_content` calls `validate_sender_info` after the envelope signature check: [3](#0-2) 

`verify_sender_info_canister_sig` checks only three things: (1) `sender_pubkey` is a valid canister signature public key, (2) the canister ID in `sender_pubkey` matches the declared `signer`, and (3) the canister signature over `SenderInfoContent(&sender_info.info)` is cryptographically valid. It never checks that the `info` blob is bound to the current request's `message_id`, `canister_id`, `method_name`, or `ingress_expiry`: [4](#0-3) 

**Canister consumption — `sender_info` drives authorization decisions:**

Canisters read the attested blob via `ic0_msg_caller_info_data_copy` and `ic0_msg_caller_info_signer_copy` system APIs and use it in `inspect_message`, update, query, and composite-query handlers to make access-control decisions: [5](#0-4) 

**Exploit flow:**

1. Attacker (user A, canister-signature public key K derived from canister C + seed S) obtains a valid `sender_info = {info: I, signer: C, sig: σ}` where σ is a canister signature from C over `\x0Eic-sender-info ‖ I`. This is obtained legitimately for request R1 targeting canister X / method M1.
2. Attacker constructs a completely different request R2 targeting canister Y / method M2 (e.g., a privileged transfer or admin action), using the same `sender_pubkey = K` and the same `sender_info = {info: I, signer: C, sig: σ}`.
3. The envelope signature over `message_id(R2)` is freshly computed by the attacker (they control key K).
4. `validate_request_content` accepts R2: the envelope signature is valid, and `verify_sender_info_canister_sig` accepts σ because it only verifies the signature over I — it does not check that I was intended for R2.
5. Canister Y receives `msg_caller_info_data() = I` and `msg_caller_info_signer() = C`, and grants the privileges encoded in I.

The same `sender_info` is also replayable across subnets via the `additional_root_of_trust` fallback path in `verify_canister_sig_with_fallback!`: [6](#0-5) 

## Impact Explanation

Any canister that uses `ic0_msg_caller_info_data_copy` / `ic0_msg_caller_info_signer_copy` to gate privileged actions (e.g., KYC-gated DeFi operations, one-time authorizations, per-transaction approvals attested by Internet Identity) is vulnerable to replay. An attacker who legitimately obtains a `sender_info` attestation for any low-value or already-executed request can reuse it indefinitely for any other request using the same canister signature public key — including requests to entirely different canisters and methods. The canister has no protocol-level mechanism to detect the replay because the signed content carries no request context. This constitutes an **ingress authorization bypass** for any canister relying on `sender_info` for request-scoped access control, matching the High impact category: unauthorized access to wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work.

## Likelihood Explanation

The `sender_info` feature is explicitly designed for use by Internet Identity and similar identity canisters to attest user attributes to application canisters. The system API (`ic0_msg_caller_info_data_copy`) is already wired into the execution environment and exposed to all canisters. Any canister developer who follows the natural pattern of "check `sender_info` to authorize this call" will be vulnerable. The attacker needs only a valid user identity and the ability to submit ingress messages — no privileged access is required. The replay requires zero cryptographic forgery.

## Recommendation

Bind the `sender_info` signature to the specific request by including the `message_id` in the signed content. Concretely, `SenderInfoContent` should be extended to include the request's `MessageId` alongside the `info` bytes:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub message_id: &'a MessageId,   // binds to the specific request
}
```

The signed bytes would then be `\x0Eic-sender-info ‖ message_id_bytes ‖ info_bytes`, making each `sender_info` valid for exactly one request. `verify_sender_info_canister_sig` must be updated to reconstruct `SenderInfoContent` using the request's `message_id` and verify against it.

## Proof of Concept

```
# Step 1: obtain a valid sender_info for a benign request R1
sender_info = {
    info:   b"role=admin",
    signer: C,
    sig:    σ = canister_sig(C, "\x0Eic-sender-info" || b"role=admin")
}

# Step 2: craft a privileged request R2 to a different canister Y / method "transfer"
R2 = HttpCanisterUpdate {
    canister_id:   Y,
    method_name:   "transfer",
    arg:           encode(amount=1_000_000),
    sender:        principal_from_key(K),
    ingress_expiry: now + 5min,
    sender_info:   sender_info,   # SAME sender_info, no modification needed
}

# Step 3: sign R2's message_id with key K (attacker controls K)
sig_R2 = sign(K, "\x0Aic-request" || message_id(R2))

# Step 4: submit envelope — passes validate_request_content because:
#   - envelope sig over message_id(R2) is valid (attacker signed it)
#   - verify_sender_info_canister_sig checks σ over b"role=admin" only — PASSES
#   - canister Y sees msg_caller_info_data() = b"role=admin" and grants admin access
```

A deterministic integration test can be written using `PocketIC` or the existing `ic_validator_http_request_test_utils` harness: create two distinct `HttpCanisterUpdate` requests with different `canister_id`/`method_name`, attach the same `sender_info` (signed by a test canister signer), and assert that `validate_request` accepts both — demonstrating the replay without any cryptographic forgery.

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

**File:** rs/validator/src/ingress_validation.rs (L196-221)
```rust
fn validate_request_content<C: HttpRequestContent, R: RootOfTrustProvider>(
    request: &HttpRequest<C>,
    ingress_signature_verifier: &dyn IngressSigVerifier,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    validate_nonce(request)?;
    // Validate the envelope signature first (cheap check) before performing
    // expensive canister signature verification in validate_sender_info.
    let targets = validate_user_id_and_signature(
        ingress_signature_verifier,
        &request.sender(),
        &request.id(),
        match request.authentication() {
            Authentication::Anonymous => None,
            Authentication::Authenticated(signature) => Some(signature),
        },
        current_time,
        root_of_trust_provider,
    )?;
    validate_sender_info(request, ingress_signature_verifier, root_of_trust_provider)?;
    Ok(targets)
}
```

**File:** rs/validator/src/ingress_validation.rs (L439-457)
```rust
macro_rules! verify_canister_sig_with_fallback {
    ($validator:expr, $sig:expr, $message:expr, $pk:expr, $root_provider:expr,
     $map_verify_err:expr, $map_root_err:expr) => {{
        let verified_with_additional =
            $root_provider
                .additional_root_of_trust()
                .is_some_and(|additional_root_of_trust| {
                    $validator
                        .verify_canister_sig($sig, $message, $pk, &additional_root_of_trust)
                        .is_ok()
                });
        if !verified_with_additional {
            let root_of_trust = $root_provider.root_of_trust().map_err($map_root_err)?;
            $validator
                .verify_canister_sig($sig, $message, $pk, &root_of_trust)
                .map_err($map_verify_err)?;
        }
    }};
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

**File:** rs/interfaces/src/execution_environment.rs (L833-857)
```rust
    /// Returns the size of the caller info data blob.
    fn ic0_msg_caller_info_data_size(&self) -> HypervisorResult<usize>;

    /// Copies `size` bytes starting from `offset` inside the caller info data blob
    /// to heap[dst..dst+size].
    fn ic0_msg_caller_info_data_copy(
        &self,
        dst: usize,
        offset: usize,
        size: usize,
        heap: &mut [u8],
    ) -> HypervisorResult<()>;

    /// Returns the size of the caller info signer blob.
    fn ic0_msg_caller_info_signer_size(&self) -> HypervisorResult<usize>;

    /// Copies `size` bytes starting from `offset` inside the caller info signer blob
    /// to heap[dst..dst+size].
    fn ic0_msg_caller_info_signer_copy(
        &self,
        dst: usize,
        offset: usize,
        size: usize,
        heap: &mut [u8],
    ) -> HypervisorResult<()>;
```
