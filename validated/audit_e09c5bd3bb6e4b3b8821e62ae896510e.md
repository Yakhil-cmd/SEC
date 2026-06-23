### Title
`SenderInfoContent` Canister Signature Not Bound to Target Canister — Cross-Canister Replay of Attestations - (File: `rs/types/types/src/messages/http.rs`)

---

### Summary

The `sender_info` feature allows a signing canister (e.g., Internet Identity) to attest to user attributes by producing a canister signature over a raw `info` blob. The signed content (`SenderInfoContent`) contains only the `info` bytes and a domain separator — it does not include the target canister ID, the sender principal, the method name, or any other request-specific context. This means a valid `sender_info` attestation obtained for one canister can be replayed verbatim in a request to a completely different canister, analogous to the Tracer `targetTracer` mismatch where a signed order for market A is executable in market B.

---

### Finding Description

`SenderInfoContent` is defined as: [1](#0-0) 

Its `write_signed_bytes_without_domain_separator` implementation appends only the raw `info` bytes — no canister ID, no sender, no method, no expiry. The canister signature therefore commits to:

- The signing canister's identity (via the canister signature public key)
- The seed embedded in that public key
- The `info` blob content

It does **not** commit to the target canister ID or any other request-specific field.

The validator in `verify_sender_info_canister_sig` enforces three things: [2](#0-1) 

1. `sender_pubkey` is a valid canister signature public key.
2. The canister ID in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid.

There is no step that checks whether the `info` blob (or the signature) is bound to the canister being called. The outer envelope signature **is** bound to the full request (via the `MessageId` representation-independent hash, which includes `sender_info` fields): [3](#0-2) 

But the `sender_info` canister signature itself is not. An attacker can take a valid `(info, signer, sig)` triple obtained for a request to canister A and embed it unchanged in a fresh, correctly signed request to canister B. The outer signature for the B-request is freshly computed by the user's key over B's `MessageId`, so it is valid. The `sender_info` canister signature is also valid because it only covers the `info` bytes.

---

### Impact Explanation

Any canister that uses `msg_caller_info_data()` / `msg_caller_info_signer()` for access-control decisions (e.g., "accept only requests attested by Internet Identity as premium users") can be bypassed. An attacker who legitimately obtains a `sender_info` attestation for canister A can replay it to canister B without the signing canister's knowledge or consent. The target canister B receives a cryptographically valid attestation that was never intended for it, potentially granting the attacker elevated privileges, bypassing per-canister authorization policies, or impersonating a higher-trust identity context.

The `SenderInfo` struct that reaches execution contains only `info` and `signer`: [4](#0-3) 

The executing canister has no protocol-level guarantee that the attestation was issued for the canister it is running in.

---

### Likelihood Explanation

The `sender_info` feature is production-ready and actively tested end-to-end: [5](#0-4) 

Any unprivileged user who can make a legitimate call to one canister that accepts `sender_info` can obtain a valid `(info, signer, sig)` triple. Replaying it to a second canister requires only constructing a fresh envelope with a new `ingress_expiry` and re-signing the outer message — standard HTTP API operations. No privileged access, no key compromise, and no threshold-majority corruption is required.

---

### Recommendation

Bind the `SenderInfoContent` signed bytes to the target canister ID (and optionally the sender principal) so that a signature produced for canister A cannot be accepted by canister B. Concretely, `write_signed_bytes_without_domain_separator` should serialize the target `canister_id` before the `info` bytes:

```rust
// In SenderInfoContent, include the target canister ID:
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub canister_id: &'a CanisterId,
}
```

The validator `verify_sender_info_canister_sig` must then receive and pass the request's `canister_id` when constructing the signable content, mirroring how `validate_request_target` already enforces that the request's `canister_id` is within the delegation target set: [6](#0-5) 

---

### Proof of Concept

1. Deploy signing canister **C** (e.g., Internet Identity) on the IC.
2. Deploy two application canisters **A** and **B**, both checking `msg_caller_info_signer()` for access control.
3. User **U** (whose `sender_pubkey` is a canister signature key from **C**) sends a valid request to **A** with `sender_info = {info: b"premium", signer: C, sig: C.sign(SenderInfoContent(b"premium"))}`. This is accepted.
4. **U** constructs a fresh request to **B** with a new `ingress_expiry` and a new outer signature over **B**'s `MessageId`, but with the **identical** `sender_info` triple from step 3.
5. The replica accepts the request to **B**: `verify_sender_info_canister_sig` passes because the canister signature over `SenderInfoContent(b"premium")` is still valid, and no check compares `sender_info` to **B**'s canister ID.
6. Canister **B** receives `msg_caller_info_data() = b"premium"` and `msg_caller_info_signer() = C`, granting **U** the same elevated access as in **A**, even though **C** never issued an attestation for **B**. [7](#0-6) [8](#0-7)

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

**File:** rs/validator/src/ingress_validation.rs (L223-234)
```rust
fn validate_request_target<C: HasCanisterId>(
    request: &HttpRequest<C>,
    targets: &CanisterIdSet,
) -> Result<(), RequestValidationError> {
    if targets.contains(&request.content().canister_id()) {
        Ok(())
    } else {
        Err(CanisterNotInDelegationTargets(
            request.content().canister_id(),
        ))
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L490-544)
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
```

**File:** rs/types/types/src/messages/ingress_messages.rs (L112-130)
```rust
impl HttpRequestContent for SignedIngressContent {
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

**File:** rs/tests/crypto/ingress_verification_test.rs (L1184-1217)
```rust
/// Tests that requests with valid canister-signed sender_info are accepted,
/// and that various forms of invalid sender_info are rejected.
pub fn requests_with_valid_sender_info(env: TestEnv) {
    let logger = env.logger();
    let node = env.get_first_healthy_node_snapshot();
    let agent = node.build_default_agent();
    block_on({
        async move {
            let node_url = node.get_public_url();
            debug!(logger, "Selected replica"; "url" => format!("{}", node_url));

            let canister =
                UniversalCanister::new_with_retries(&agent, node.effective_canister_id(), &logger)
                    .await;
            let test_info = TestInformation {
                url: node_url,
                canister_id: canister_id_from_principal(&canister.canister_id()),
            };

            let seed = b"sender_info_test_seed".to_vec();
            let signer = CanisterSigner::new(&canister, seed);
            let id = GenericIdentity::new_canister(signer.clone());

            // The info blob that the signing canister attests to.
            let info_bytes = b"some user attributes".to_vec();
            let sender_info_content = SenderInfoContent(&info_bytes);
            let sender_info_signed_bytes = sender_info_content.as_signed_bytes();
            let sender_info_sig = signer.sign(&sender_info_signed_bytes).await;

            let valid_sender_info = || RawSignedSenderInfo {
                info: Blob(info_bytes.clone()),
                signer: Blob(signer.canister_id().get().as_slice().to_vec()),
                sig: Blob(sender_info_sig.clone()),
            };
```
