### Title
NI-DKG FS Encryption PoP Accepts G1 Identity Element, Enabling Trivial Share Recovery - (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/encryption_key_pop.rs`)

### Summary

The `prove_pop` / `verify_pop` Schnorr PoP scheme for NI-DKG forward-secure encryption keys does not reject the G1 identity element (point at infinity) as a public key. A malicious node operator can register the identity as their FS encryption public key, pass all validation, and cause any DKG dealing encrypted to that key to produce a ciphertext whose plaintext chunk is trivially recoverable by any observer.

### Finding Description

**Step 1 — `prove_pop` accepts `(pk=identity, witness=0)`**

In `prove_pop`, the only instance validity check is:

```rust
if instance.public_key != (&instance.g1_gen * witness).to_affine() {
    return Err(EncryptionKeyPopError::InvalidInstance);
}
``` [1](#0-0) 

With `witness = Scalar::zero()` and `public_key = G1Affine::identity()`, this evaluates to `identity != (g1 * 0).to_affine()` → `identity != identity` → `false`, so no error is returned. The prover then computes `pop_key = pop_base * 0 = identity`, `response = challenge * 0 + random_scalar = random_scalar`, and returns a valid-looking PoP.

**Step 2 — `verify_pop` passes for the identity key**

During verification:

```rust
let blinder_public_key = G1Projective::mul2_affine_vartime(
    &instance.public_key,  // identity
    &minus_challenge,
    &instance.g1_gen,
    &pop.response,
);
``` [2](#0-1) 

`identity * (-challenge) + g1 * response = g1 * random_scalar`, which exactly matches what the prover committed to. The challenge recomputes identically, so `verify_pop` returns `Ok(())`.

**Step 3 — `G1Affine::deserialize` accepts the identity**

The identity element has a well-defined compressed encoding (`0xc0` followed by 47 zero bytes). The test suite confirms it round-trips through `G1Affine::deserialize`: [3](#0-2) 

`PublicKeyWithPop::deserialize` calls `G1Affine::deserialize(pk.as_bytes())`, so the identity is accepted. [4](#0-3) 

**Step 4 — `ValidDkgDealingEncryptionPublicKey::try_from` passes**

The validation calls `fs_ni_dkg_pubkey.verify(node_id.get().as_slice())`, which calls `verify_pop` — which passes as shown above. [5](#0-4) 

**Step 5 — `enc_chunks` leaks the plaintext**

When encrypting to the identity public key:

```rust
let pk_g1_tbl = G1Projective::compute_mul2_affine_tbl(pk, g1);
let enc_chunks = G1Projective::batch_normalize_array(&pk_g1_tbl.mul2_array(&r, &chunks));
``` [6](#0-5) 

This computes `identity * r + g1 * chunk = g1 * chunk`. The ciphertext chunk is simply the plaintext chunk encoded as a G1 point. Since chunks are 16-bit values (`CHUNK_BYTES = 2`, `NUM_CHUNKS = 16`), any observer can recover the chunk via a baby-step giant-step discrete log over the small range `[0, 65535]`.

### Impact Explanation

Any DKG dealing encrypted to the malicious node's identity key has that node's secret share trivially recoverable from the public ciphertext. The share is a 256-bit scalar split into 16-bit chunks, each of which is independently recoverable. This violates the confidentiality guarantee of the NI-DKG protocol for that node's share.

### Likelihood Explanation

Requires a malicious node operator (governance-approved) to register the identity as their FS encryption public key. This is within the IC's threat model for nodes below the fault threshold. The PoP — the sole cryptographic guard against this — fails to reject the identity. No other check in the validation chain (`ValidDkgDealingEncryptionPublicKey::try_from`, `fs_ni_dkg_pubkey_from_proto`, `PublicKeyWithPop::deserialize`) rejects the identity element. [7](#0-6) 

### Recommendation

Add an explicit identity-element rejection in `prove_pop` and/or `verify_pop`:

```rust
if instance.public_key.is_identity() {
    return Err(EncryptionKeyPopError::InvalidInstance);
}
```

Also add the same check in `ValidDkgDealingEncryptionPublicKey::try_from` before calling `verify`.

### Proof of Concept

```rust
let rng = &mut reproducible_rng();
let identity = G1Affine::identity();
let witness = Scalar::zero();
let associated_data = b"test-node-id";
let instance = EncryptionKeyInstance::new(&identity, associated_data);

// prove_pop succeeds with witness=0 and pk=identity
let pop = prove_pop(&instance, &witness, rng).expect("should succeed");

// verify_pop passes
assert_eq!(verify_pop(&instance, &pop), Ok(()));

// enc_chunks to identity leaks plaintext
let chunk_val = 42u64;
let chunk = Scalar::from_u64(chunk_val);
let ptext = PlaintextChunks::from_scalar(&chunk);
let sys = SysParam::global();
let (crsz, _) = enc_chunks(&[(identity, ptext)], Epoch::from(0), associated_data, sys, rng);
// crsz.cc[0][i] == g1 * chunk_i — recoverable by dlog in [0, 65535]
```

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/encryption_key_pop.rs (L102-105)
```rust
    // Check validity of the instance
    if instance.public_key != (&instance.g1_gen * witness).to_affine() {
        return Err(EncryptionKeyPopError::InvalidInstance);
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/encryption_key_pop.rs (L143-151)
```rust
    let blinder_public_key = G1Projective::mul2_affine_vartime(
        &instance.public_key,
        &minus_challenge,
        &instance.g1_gen,
        &pop.response,
    );

    let blinder_pop_key =
        G1Projective::mul2_affine_vartime(&pop.pop_key, &minus_challenge, &pop_base, &pop.response);
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/type/tests/tests.rs (L760-792)
```rust
    /// The additive identity, also known as zero.
    const INFINITY: &str = "c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
    /// Powers of 2: `g1_generator * {1, 2, 4, 8, ...}`
    const POWERS_OF_2: &[&str] = &[
        GENERATOR,
        "a572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "ac9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "a85ae765588126f5e860d019c0e26235f567a9c0c0b2d8ff30f3e8d436b1082596e5e7462d20f5be3764fd473e57f9cf",
        "a73eb991aa22cdb794da6fcde55a427f0a4df5a4a70de23a988b5e5fc8c4d844f66d990273267a54dd21579b7ba6a086",
        "a72841987e4f219d54f2b6a9eac5fe6e78704644753c3579e776a3691bc123743f8c63770ed0f72a71e9e964dbf58f43",
    ];
    /// Positive numbers: `g1_generator * {1,2,3,4,...}`
    const POSITIVE_NUMBERS: &[&str] = &[
        GENERATOR,
        "a572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "89ece308f9d1f0131765212deca99697b112d61f9be9a5f1f3780a51335b3ff981747a0b2ca2179b96d2c0c9024e5224",
        "ac9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "b0e7791fb972fe014159aa33a98622da3cdc98ff707965e536d8636b5fcc5ac7a91a8c46e59a00dca575af0f18fb13dc",
    ];
    /// Negative numbers: `g1_generator * {-1, -2, -3, -4, ...}`
    const NEGATIVE_NUMBERS: &[&str] = &[
        "b7f1d3a73197d7942695638c4fa9ac0fc3688c4f9774b905a14e3a3f171bac586c55e83ff97a1aeffb3af00adb22c6bb",
        "8572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "a9ece308f9d1f0131765212deca99697b112d61f9be9a5f1f3780a51335b3ff981747a0b2ca2179b96d2c0c9024e5224",
        "8c9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "90e7791fb972fe014159aa33a98622da3cdc98ff707965e536d8636b5fcc5ac7a91a8c46e59a00dca575af0f18fb13dc",
    ];

    let g = G1Affine::generator();
    let identity = G1Affine::identity();

    g1_test_encoding(identity.clone(), INFINITY);
    g1_test_encoding(g.clone(), GENERATOR);
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L272-285)
```rust
    pub fn deserialize(pk: &FsEncryptionPublicKey, pop: &FsEncryptionPop) -> Option<Self> {
        let key_value = G1Affine::deserialize(pk.as_bytes());
        let pop_key = G1Affine::deserialize(&pop.pop_key);
        let challenge = Scalar::deserialize(&pop.challenge);
        let response = Scalar::deserialize(&pop.response);

        match (key_value, pop_key, challenge, response) {
            (Ok(key_value), Ok(pop_key), Ok(challenge), Ok(response)) => Some(Self {
                key_value,
                proof_data: EncryptionKeyPop::new(pop_key, challenge, response),
            }),
            (_, _, _, _) => None,
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L713-721)
```rust
        for (pk, ptext) in recipient_and_message {
            let pk_g1_tbl = G1Projective::compute_mul2_affine_tbl(pk, g1);

            let chunks = ptext.chunks_as_scalars();

            let enc_chunks =
                G1Projective::batch_normalize_array(&pk_g1_tbl.mul2_array(&r, &chunks));

            cc.push(enc_chunks);
```

**File:** rs/crypto/node_key_validation/src/lib.rs (L326-335)
```rust
    fn try_from((public_key, node_id): (PublicKey, NodeId)) -> Result<Self, Self::Error> {
        // Note: `fs_ni_dkg_pubkey_from_proto` also ensures that the
        // public key is a point on the curve and in the right subgroup.
        let fs_ni_dkg_pubkey = fs_ni_dkg_pubkey_from_proto(&public_key)
            .map_err(|e| invalid_dkg_dealing_enc_pubkey_error(format!("{e}")))?;
        if !fs_ni_dkg_pubkey.verify(node_id.get().as_slice()) {
            return Err(invalid_dkg_dealing_enc_pubkey_error("verification failed"));
        }
        Ok(Self { public_key })
    }
```

**File:** rs/crypto/node_key_validation/src/proto_conversions/fs_ni_dkg.rs (L11-38)
```rust
pub fn fs_ni_dkg_pubkey_from_proto(
    pubkey_proto: &PublicKeyProto,
) -> Result<ClibFsNiDkgPublicKey, FsNiDkgPubkeyFromPubkeyProtoError> {
    let csp_pk = CspFsEncryptionPublicKey::try_from(pubkey_proto.clone()).map_err(|e| {
        FsNiDkgPubkeyFromPubkeyProtoError::PublicKeyConversion {
            error: format!("{e}"),
        }
    })?;
    let csp_pop = CspFsEncryptionPop::try_from(pubkey_proto).map_err(|e| {
        FsNiDkgPubkeyFromPubkeyProtoError::PopConversion {
            error: format!("{e}"),
        }
    })?;
    clib_fs_ni_dkg_pubkey_from_csp_pubkey_with_pop(&csp_pk, &csp_pop)
        .ok_or(FsNiDkgPubkeyFromPubkeyProtoError::InternalConversion)
}

fn clib_fs_ni_dkg_pubkey_from_csp_pubkey_with_pop(
    csp_pubkey: &CspFsEncryptionPublicKey,
    csp_pop: &CspFsEncryptionPop,
) -> Option<ClibFsNiDkgPublicKey> {
    match (csp_pubkey, csp_pop) {
        (
            CspFsEncryptionPublicKey::Groth20_Bls12_381(pubkey),
            CspFsEncryptionPop::Groth20WithPop_Bls12_381(pop),
        ) => ClibFsNiDkgPublicKey::deserialize(pubkey, pop),
    }
}
```
