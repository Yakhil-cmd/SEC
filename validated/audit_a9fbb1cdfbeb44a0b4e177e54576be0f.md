Audit Report

## Title
Unbounded CPU Cost in Delegation Chain Validation via COSE RSA-8192 Keys — (`rs/validator/src/ingress_validation.rs`, `rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`)

## Summary
An unprivileged sender can submit an ingress message with up to 20 COSE-wrapped RSA-8192 delegation keys. Each hop in the chain triggers a full RSA-8192 PKCS#1 v1.5 modular exponentiation in `verify_pkcs1_sha256` with no per-chain CPU budget. Flooding the replica with such messages saturates the `spawn_blocking` thread pool used for ingress validation, causing application-level DoS of ingress processing without requiring raw volumetric traffic.

## Finding Description
`MAXIMUM_NUMBER_OF_DELEGATIONS = 20` is the only chain-length guard; no computational cost budget exists. [1](#0-0) 

`validate_delegations` iterates over all delegations unconditionally, calling `validate_delegation` for each. [2](#0-1) 

Inside `validate_delegation`, a `RsaSha256PublicKeyDerWrappedCose` key type is routed to `validate_webauthn_sig`, which invokes `verify_pkcs1_sha256`. [3](#0-2) 

`verify_pkcs1_sha256` performs an unconditional RSA modular exponentiation with no budget check. [4](#0-3) 

RSA-8192 keys are explicitly permitted by `MAXIMUM_RSA_KEY_SIZE = 8192`. [5](#0-4) 

The COSE branch in `user_public_key_from_bytes` parses the inner RSA key via `RsaPublicKey::from_der_spki`. [6](#0-5) 

Validation runs inside `tokio::task::spawn_blocking` with no visible concurrency cap at the call site. [7](#0-6) 

The only pre-validation guard is a byte-size check, not a computational-cost check. [8](#0-7) 

A ~44 KB payload (20 × ~1.5 KB COSE RSA-8192 keys + 20 × 1 KB signatures) fits well within the 2 MB ingress limit, so the size guard provides no protection. The attacker generates keys and signs delegations offline once; the replica bears the full 20× RSA-8192 verification cost on every submission.

## Impact Explanation
RSA-8192 verification is roughly 16× more expensive than RSA-2048 and orders of magnitude more expensive than Ed25519. Twenty such verifications per ingress message, submitted concurrently by an attacker, saturates the `spawn_blocking` thread pool, starving legitimate ingress validation and degrading subnet throughput. This is an application/platform-level DoS not based on raw volumetric DDoS, matching the **High ($2,000–$10,000)** impact tier: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation
No privileged access is required; any principal can submit ingress messages with delegations. RSA-8192 key generation and signing is expensive but performed once offline; the resulting payload is replayable within TTL or re-submitted with a fresh expiry. No rate-limiting on per-request cryptographic cost exists in the validation path. The attack is straightforward to automate.

## Recommendation
1. **Enforce a per-chain CPU budget**: Assign weights per key type (e.g., Ed25519 = 1, ECDSA-P256 = 2, RSA-2048 = 50, RSA-8192 = 200) and reject chains exceeding a threshold before any cryptographic operation.
2. **Restrict RSA key sizes in delegations**: Cap delegation keys at RSA-2048 or RSA-4096, or disallow RSA COSE keys in delegation chains entirely (WebAuthn RSA is primarily needed for the final signing key, not intermediate delegation keys).
3. **Add concurrency limiting** on the `spawn_blocking` ingress validation pool with back-pressure to prevent CPU saturation.

## Proof of Concept
```rust
// Local replica or PocketIC integration test:
// 1. Generate 21 RSA-8192 key pairs offline.
// 2. Build a 20-hop delegation chain: each delegation[i] is signed by keys[i]
//    and delegates to keys[i+1], with pubkey encoded as COSE DER-wrapped RSA-8192.
// 3. Submit an ingress call with sender_pubkey = keys[0].cose_der_wrapped_pubkey()
//    and sender_delegation = the 20-hop chain.
// 4. Measure wall-clock time for validate_request vs. an equivalent Ed25519 chain.
// 5. Assert ratio > 100x; assert that concurrent submission of N such messages
//    causes ingress validation latency for legitimate Ed25519 messages to exceed
//    an acceptable threshold (e.g., 10x baseline).
//
// All code paths exercised are production paths confirmed above.
// No mainnet testing required; reproducible on a local replica fork.
```

### Citations

**File:** rs/validator/src/ingress_validation.rs (L36-36)
```rust
const MAXIMUM_NUMBER_OF_DELEGATIONS: usize = 20;
```

**File:** rs/validator/src/ingress_validation.rs (L735-750)
```rust
    for sd in signed_delegations {
        let delegation = sd.delegation();
        let signature = sd.signature();

        let new_targets = validate_delegation(
            validator,
            signature,
            delegation,
            &pubkey,
            root_of_trust_provider,
        )
        .map_err(InvalidDelegation)?;
        // Restrict the canister targets to the ones specified in the delegation.
        targets = targets.intersect(new_targets);
        pubkey = delegation.pubkey().to_vec();
    }
```

**File:** rs/validator/src/ingress_validation.rs (L803-808)
```rust
        KeyBytesContentType::EcdsaP256PublicKeyDerWrappedCose
        | KeyBytesContentType::Ed25519PublicKeyDerWrappedCose
        | KeyBytesContentType::RsaSha256PublicKeyDerWrappedCose => {
            let webauthn_sig = WebAuthnSignature::try_from(signature).map_err(WebAuthnError)?;
            validate_webauthn_sig(validator, &webauthn_sig, delegation, &pk)
                .map_err(WebAuthnError)?;
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L37-38)
```rust
    pub const MINIMUM_RSA_KEY_SIZE: usize = 2048;
    pub const MAXIMUM_RSA_KEY_SIZE: usize = 8192;
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L152-167)
```rust
    pub fn verify_pkcs1_sha256(&self, message: &[u8], signature: &[u8]) -> CryptoResult<()> {
        let digest = Sha256::hash(message);

        match &self
            .key
            .verify(Pkcs1v15Sign::new::<sha2::Sha256>(), &digest, signature)
        {
            Ok(_) => Ok(()),
            Err(e) => Err(CryptoError::SignatureVerification {
                algorithm: AlgorithmId::RsaSha256,
                public_key_bytes: self.as_der().to_vec(),
                sig_bytes: signature.to_vec(),
                internal_error: format!("{e:?}"),
            }),
        }
    }
```

**File:** rs/crypto/standalone-sig-verifier/src/sign_utils.rs (L87-97)
```rust
    } else if pkix_algo_id == cose_algorithm_identifier() {
        let (alg_id, bytes) = cose::parse_cose_public_key(&pk_der)?;
        let key_bytes = user_public_key_from_bytes(&bytes)?;
        let key_contents_type = cose_key_bytes_content_type(alg_id).ok_or_else(|| {
            CryptoError::AlgorithmNotSupported {
                algorithm: alg_id,
                reason: "cose_key_bytes_content_type needs to be updated for this algorithm"
                    .to_string(),
            }
        })?;
        (key_bytes.0.key, alg_id, key_contents_type)
```

**File:** rs/http_endpoints/public/src/call.rs (L302-312)
```rust
        if msg.count_bytes() > ingress_registry_settings.max_ingress_bytes_per_message {
            Err(HttpError {
                status: StatusCode::PAYLOAD_TOO_LARGE,
                message: format!(
                    "Request {} is too large. Message byte size {} is larger than the max allowed {}.",
                    message_id,
                    msg.count_bytes(),
                    ingress_registry_settings.max_ingress_bytes_per_message
                ),
            })?;
        }
```

**File:** rs/http_endpoints/public/src/call.rs (L327-333)
```rust
        tokio::task::spawn_blocking(move || {
            validator.validate_request(
                &request_c,
                time_source.get_relative_time(),
                &root_of_trust_provider,
            )
        })
```
