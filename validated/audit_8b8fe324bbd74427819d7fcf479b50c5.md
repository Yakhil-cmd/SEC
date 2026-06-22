Looking at the actual code in the repository to trace the full call chain and assess whether the preconditions hold.

**Call chain verification:** [1](#0-0) [2](#0-1) 

`parse_pk` calls `simple_asn1::from_der` directly with no depth guard.

**Ingress path to this code:** [3](#0-2) [4](#0-3) 

`public_key_from_bytes` → `user_public_key_from_bytes` → `algo_id_and_public_key_bytes_from_der` → `parse_pk` → `simple_asn1::from_der`.

**`validate_user_id` does not block the attack:** [5](#0-4) 

This only computes `SHA-224(sender_pubkey)` and compares it to the declared sender. An attacker sets `sender = new_self_authenticating(malicious_der_bytes)`, which trivially passes. DER parsing is not invoked here.

**No public-key size limit before DER parsing:** There is no check on the byte length of `signer_pubkey` before `simple_asn1::from_der` is called.

**Message size limit does not prevent deep nesting:** [6](#0-5) 

A DER structure with N levels of nesting `SEQUENCE { SEQUENCE { ... } }` requires only ~2N bytes (tag + short-form length per level). 10,000 levels ≈ 20 KB; 100,000 levels ≈ 200 KB — both well within the 2 MB app-subnet limit.

**`simple_asn1::from_der` is recursive:** The `simple_asn1` crate (all versions present in this codebase) implements `from_der` via a mutually recursive `decode_der` / `decode_one` pair. When `decode_one` encounters a `SEQUENCE` tag it calls `decode_der` on the contents, which calls `decode_one` again — one stack frame per nesting level, with no depth counter.

**Stack overflow kills the process:** In Rust, a stack overflow triggers `SIGABRT` / process abort, not a recoverable panic. The `spawn_blocking` wrapper in the HTTP endpoint does not change this. [7](#0-6) 

The ingress-handler path also calls `validate_request` synchronously: [8](#0-7) 

---

### Title
Unbounded recursive `simple_asn1::from_der` on attacker-controlled `signer_pubkey` causes stack overflow and replica crash — (`rs/crypto/internal/crypto_lib/basic_sig/der_utils/src/lib.rs`)

### Summary
An unprivileged sender can submit an ingress message whose `sender_pubkey` field contains a DER-encoded byte string with arbitrarily deep ASN.1 SEQUENCE nesting. `simple_asn1::from_der` parses this recursively without a depth limit, exhausting the thread stack and aborting the replica process.

### Finding Description
`KeyDerParser::parse_pk` calls `simple_asn1::from_der` unconditionally on the raw public-key bytes supplied by the caller. [2](#0-1) 

`simple_asn1::from_der` is a recursive descent parser: each `SEQUENCE` tag causes a re-entrant call to parse its contents. No depth counter or stack-guard exists anywhere in the chain from ingress receipt to this call. The structural checks (`ensure_single_asn1_sequence`, `key_seq.len() != 2`) execute only after `from_der` returns — they cannot prevent the overflow.

### Impact Explanation
A stack overflow in Rust is an unrecoverable process abort. Crashing the replica process on one node removes that node from the subnet for the duration of the restart. Repeated requests targeting multiple nodes could reduce the live replica count below the fault-tolerance threshold, stalling the subnet. Even a single-node crash is a denial-of-service against that replica.

### Likelihood Explanation
The attack requires only a valid HTTP ingress call with a crafted `sender_pubkey`. No authentication, no cycles, no privileged role. The sender ID check is bypassed by deriving the sender principal from the malicious key bytes. The crafted payload is small (tens of kilobytes) and passes the message-size check.

### Recommendation
1. Add a recursion-depth limit (e.g., 16) before calling `simple_asn1::from_der`, or replace it with an iterative DER parser that enforces a depth cap.
2. Add a maximum byte-length check on `signer_pubkey` (e.g., 512 bytes) before any parsing, since legitimate SubjectPublicKeyInfo structures for all supported algorithms are well under 1 KB.
3. Consider running ingress signature validation in a thread with a reduced, explicitly sized stack so that a stack overflow is contained to that thread rather than aborting the process.

### Proof of Concept
```python
# Build: SEQUENCE { SEQUENCE { ... (10_000 levels) ... } }
def make_nested_seq(depth):
    inner = b'\x30\x00'          # innermost empty SEQUENCE
    for _ in range(depth - 1):
        inner = b'\x30' + encode_length(len(inner)) + inner
    return inner

payload = make_nested_seq(10_000)   # ~20 KB, well under 2 MB limit
sender  = sha224(b'\x02' + payload) + b'\x02'  # self-authenticating principal

# Submit as a signed ingress call with:
#   sender      = derived principal above
#   sender_pubkey = payload
#   sender_sig  = arbitrary bytes (parsing fails before sig check)
```
Fuzzing with increasing depth and measuring RSS/stack consumption will confirm the overflow threshold.

### Citations

**File:** rs/crypto/internal/crypto_lib/basic_sig/der_utils/src/lib.rs (L95-100)
```rust
pub fn algo_id_and_public_key_bytes_from_der(
    der: &[u8],
) -> Result<(PkixAlgorithmIdentifier, Vec<u8>), KeyDerParsingError> {
    let kp = KeyDerParser::new(der);
    kp.get_algo_id_and_public_key_bytes()
}
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/der_utils/src/lib.rs (L196-199)
```rust
    fn parse_pk(&self) -> Result<Vec<ASN1Block>, KeyDerParsingError> {
        simple_asn1::from_der(&self.key_der)
            .map_err(|e| Self::parsing_error(&format!("Error in DER encoding: {e}")))
    }
```

**File:** rs/validator/src/ingress_validation.rs (L626-631)
```rust
fn validate_user_id(sender_pubkey: &[u8], id: &UserId) -> Result<(), RequestValidationError> {
    if id.get_ref() == &PrincipalId::new_self_authenticating(sender_pubkey) {
        Ok(())
    } else {
        Err(UserIdDoesNotMatchPublicKey(*id, sender_pubkey.to_vec()))
    }
```

**File:** rs/validator/src/ingress_validation.rs (L657-657)
```rust
    let (pk, pk_type) = public_key_from_bytes(&pubkey).map_err(InvalidSignature)?;
```

**File:** rs/validator/src/ingress_validation.rs (L841-877)
```rust
// Verifies correct user and signature.
fn validate_user_id_and_signature<R: RootOfTrustProvider>(
    ingress_signature_verifier: &dyn IngressSigVerifier,
    sender: &UserId,
    message_id: &MessageId,
    signature: Option<&UserSignature>,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    match signature {
        None => {
            if sender.get().is_anonymous() {
                return Ok(CanisterIdSet::all());
            }
            Err(MissingSignature(*sender))
        }
        Some(signature) => {
            if sender.get().is_anonymous() {
                Err(AnonymousSignatureNotAllowed)
            } else {
                let sender_pubkey = &signature.signer_pubkey;
                validate_user_id(sender_pubkey, sender).and_then(|()| {
                    validate_signature(
                        ingress_signature_verifier,
                        message_id,
                        signature,
                        current_time,
                        root_of_trust_provider,
                    )
                })
            }
        }
    }
}
```

**File:** rs/limits/src/lib.rs (L85-86)
```rust
pub const MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET: u64 = 2 * MEGABYTE;
pub const MAX_INGRESS_BYTES_PER_MESSAGE_NNS_SUBNET: u64 = 3 * MEGABYTE + 512 * KILOBYTE;
```

**File:** rs/http_endpoints/public/src/call.rs (L327-338)
```rust
        tokio::task::spawn_blocking(move || {
            validator.validate_request(
                &request_c,
                time_source.get_relative_time(),
                &root_of_trust_provider,
            )
        })
        .await
        .map_err(|_| HttpError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: "".into(),
        })?
```

**File:** rs/ingress_manager/src/ingress_handler.rs (L197-203)
```rust
        if let Err(err) = self.request_validator.validate_request(
            ingress_object.signed_ingress.as_ref(),
            consensus_time,
            &self.registry_root_of_trust_provider(registry_version),
        ) {
            return Err(IngressMessageValidationError::InvalidRequest(err));
        }
```
