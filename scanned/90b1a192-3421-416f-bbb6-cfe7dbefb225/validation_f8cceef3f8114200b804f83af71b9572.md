### Title
`sender_info` Canister Signature Not Bound to Request Context Enables Cross-Request Replay - (File: `rs/validator/src/ingress_validation.rs`)

### Summary

The `sender_info` field introduced in IC ingress messages allows a signing canister (e.g., Internet Identity) to attest to user attributes. The canister signature inside `sender_info` is verified only over the raw `info` bytes with domain separator `"ic-sender-info"`, with no cryptographic binding to the specific request's `canister_id`, `method_name`, `ingress_expiry`, or `nonce`. A valid `sender_info` obtained for one request can therefore be replayed verbatim in any other request by the same principal — to a different canister, a different method, or at a different time within the ingress window — bypassing any access-control logic that canisters build on top of `sender_info`.

### Finding Description

**Root cause — `verify_sender_info_canister_sig`**

In `rs/validator/src/ingress_validation.rs` the function `verify_sender_info_canister_sig` constructs the signed content as:

```rust
let sender_info_content = SenderInfoContent(&sender_info.info);
let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));
verify_canister_sig_with_fallback!(validator, &canister_sig, &sender_info_content, ...);
``` [1](#0-0) 

`SenderInfoContent` is defined as a thin wrapper that serialises only the raw `info` bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
}
``` [2](#0-1) 

The `SignatureDomain` implementation prepends only the literal `"ic-sender-info"` domain separator:

```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
    }
}
``` [3](#0-2) 

The signed bytes are therefore exactly `"\x0Eic-sender-info" || info_bytes` — no `canister_id`, no `method_name`, no `sender` principal, no `ingress_expiry`, no `nonce`.

**What the validator checks vs. what it does not check**

`verify_sender_info_canister_sig` enforces three things:
1. `sender_pubkey` is a valid canister-signature DER public key.
2. The canister ID embedded in `sender_pubkey` equals `sender_info.signer`.
3. The canister signature `sender_info.sig` is valid over `SenderInfoContent(info_bytes)`. [4](#0-3) 

It does **not** check that `info_bytes` contains any binding to the target `canister_id`, `method_name`, `ingress_expiry`, or `nonce` of the enclosing request. The `SignedIngressContent` struct carries all of those fields alongside `sender_info`, but none of them flow into the `sender_info` signature verification path. [5](#0-4) 

**Exploit path**

1. A user authenticates with Internet Identity (or any canister acting as a `sender_info` signer) and obtains a valid `RawSignedSenderInfo { info, signer, sig }` for request R₁ targeting canister A / method `read_data`.
2. The attacker constructs a new request R₂ targeting canister B / method `privileged_action`, reusing the identical `(info, signer, sig)` triple.
3. The IC ingress validator accepts R₂: the envelope signature over the new `message_id` is freshly produced by the attacker's key, and `verify_sender_info_canister_sig` passes because the canister signature over `info_bytes` is still valid.
4. Canister B receives R₂ with the `sender_info` intact and, if it gates access on the attributes in `info_bytes` (via `msg_caller_info_data()`), grants the privileged operation.

The only natural limit is the `ingress_expiry` window (≤ `MAX_INGRESS_TTL` ≈ 5 minutes), but within that window the same `sender_info` is reusable across arbitrarily many requests to arbitrarily many canisters. [6](#0-5) 

### Impact Explanation

Any canister that reads `sender_info` via the `msg_caller_info_data` system API and uses it to make authorization decisions (e.g., "caller has role X", "caller passed KYC", "caller is a premium subscriber") is vulnerable. An attacker who legitimately obtains a `sender_info` attestation for one low-privilege call can replay it against any other canister or method that trusts the same attestation, bypassing the intended access-control invariant. This is a direct analog of the reported staking-contract issue: a signature obtained for one operation is silently accepted for a different, more privileged operation.

### Likelihood Explanation

The attack requires no privileged access. Any user who has ever received a valid `sender_info` from a signing canister (the normal, intended flow) automatically possesses a replayable credential. The construction of a crafted request reusing the credential is trivial — it requires only assembling a standard IC HTTP envelope with the recycled `(info, signer, sig)` triple and a fresh envelope signature. The attack is therefore realistic for any production canister that gates privileged operations on `sender_info`.

### Recommendation

Bind the `sender_info` canister signature to the enclosing request by including request-specific context in the signed content. At minimum, the `SenderInfoContent` signable should incorporate the `canister_id` and `method_name` of the target request, and optionally the `ingress_expiry` or a per-session nonce. For example:

```rust
// Proposed: bind to (canister_id, method_name, info_bytes)
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub canister_id: &'a [u8],
    pub method_name: &'a str,
}
```

This ensures that a `sender_info` signature issued for canister A / method M cannot be replayed against canister B / method N, directly mirroring the recommendation in the external report to expire the nonce after each function call.

### Proof of Concept

```
// Attacker steps (pseudocode):

// Step 1 – obtain a legitimate sender_info for a benign call
let info_bytes = b"user_role=basic";
let sender_info_sig = internet_identity.sign("ic-sender-info" || info_bytes);
let sender_info = RawSignedSenderInfo {
    info: info_bytes,
    signer: internet_identity_canister_id,
    sig: sender_info_sig,
};

// Step 2 – construct a NEW request to a DIFFERENT canister/method
//           reusing the identical sender_info
let malicious_request = HttpRequestEnvelope {
    content: HttpCallContent::Call {
        update: HttpCanisterUpdate {
            canister_id: privileged_canister_id,   // different target
            method_name: "admin_action".to_string(), // privileged method
            arg: ...,
            sender: attacker_principal,
            ingress_expiry: fresh_expiry,
            nonce: None,
            sender_info: Some(sender_info),  // REPLAYED, unchanged
        },
    },
    sender_pubkey: Some(attacker_canister_sig_pubkey),
    sender_sig: Some(fresh_envelope_sig),  // freshly signed over new message_id
    sender_delegation: None,
};

// Step 3 – submit; verify_sender_info_canister_sig passes because
//           it only checks sig over ("ic-sender-info" || info_bytes),
//           which is identical to the original.
```

The validator at `rs/validator/src/ingress_validation.rs:459-545` will accept this request. The privileged canister receives `sender_info` with `info = b"user_role=basic"` and, if it trusts that field for authorization, will execute `admin_action` for an unprivileged user. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/validator/src/ingress_validation.rs (L549-585)
```rust
fn validate_ingress_expiry<C: HttpRequestContent>(
    request: &HttpRequest<C>,
    current_time: Time,
) -> Result<(), RequestValidationError> {
    let ingress_expiry = request.ingress_expiry();
    let provided_expiry = Time::from_nanos_since_unix_epoch(ingress_expiry);
    let min_allowed_expiry = current_time;
    // We need to account for time drift and be more forgiving at rejecting ingress
    // messages due to their expiry being too far in the future.
    // If this logic changes, then the migration canister in `//rs/migration_canister`
    // must be updated, too.
    let max_expiry_diff = MAX_INGRESS_TTL
        .checked_add(PERMITTED_DRIFT_AT_VALIDATOR)
        .ok_or_else(|| {
            InvalidRequestExpiry(format!(
                "Addition of MAX_INGRESS_TTL {MAX_INGRESS_TTL:?} with \
                PERMITTED_DRIFT_AT_VALIDATOR {PERMITTED_DRIFT_AT_VALIDATOR:?} overflows",
            ))
        })?;
    let max_allowed_expiry = min_allowed_expiry
        .checked_add(max_expiry_diff)
        .ok_or_else(|| {
            InvalidRequestExpiry(format!(
                "Addition of min_allowed_expiry {min_allowed_expiry:?} \
                with max_expiry_diff {max_expiry_diff:?} overflows",
            ))
        })?;
    if !(min_allowed_expiry <= provided_expiry && provided_expiry <= max_allowed_expiry) {
        let msg = format!(
            "Specified ingress_expiry not within expected range: \
             Minimum allowed expiry: {min_allowed_expiry}, \
             Maximum allowed expiry: {max_allowed_expiry}, \
             Provided expiry:        {provided_expiry}"
        );
        return Err(InvalidRequestExpiry(msg));
    }
    Ok(())
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

**File:** rs/types/types/src/messages/ingress_messages.rs (L42-52)
```rust
/// The contents of a signed ingress message.
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct SignedIngressContent {
    sender: UserId,
    canister_id: CanisterId,
    method_name: String,
    arg: Vec<u8>,
    ingress_expiry: u64,
    nonce: Option<Vec<u8>>,
    sender_info: Option<SignedSenderInfo>,
}
```
