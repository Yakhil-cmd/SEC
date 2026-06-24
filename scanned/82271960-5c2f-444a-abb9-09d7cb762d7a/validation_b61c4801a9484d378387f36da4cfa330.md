Let me look at the full `verify_zk_proofs` function and the chunking.rs constants to complete the analysis.

### Title
Byzantine Dealer Triggers Unbounded Memory/CPU Exhaustion via Out-of-Range Chunks in `dec_chunks` — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

---

### Summary

A Byzantine dealer node (authorized, below the collection threshold) can craft a dealing whose NIZK chunking proof passes `verify_dealing` but whose plaintext chunks fall outside `[CHUNK_MIN..CHUNK_MAX]` (i.e., `[0, 65535]`). When honest receivers call `load_transcript`, `dec_chunks` instantiates `CheatingDealerDlogSolver::new(n, m)`, which allocates up to **2 GiB** of memory and runs a full BSGS search that can take **hours per chunk**. All honest subnet replicas are affected simultaneously.

---

### Finding Description

**Constants (chunking.rs):**

`CHUNK_MIN = 0`, `CHUNK_MAX = 65535`, `CHUNK_SIZE = 65536`. [1](#0-0) 

**`dec_chunks` fallback path (forward_secure.rs lines 820–834):**

After `HonestDealerDlogLookupTable::solve_several()` returns `None` for any chunk outside `[0, 65535]`, the code unconditionally instantiates `CheatingDealerDlogSolver::new(n, m)`: [2](#0-1) 

**`CheatingDealerDlogSolver::new` resource cost (dlog_recovery.rs lines 339–366):**

`MAX_TABLE_MBYTES = 2 * 1024` (2 GiB), `LARGEST_TABLE_MUL = 20` in release mode. The BSGS table is built over a range of `2*zz - 1` where `zz = 2 * NUM_ZK_REPETITIONS * n * m * (CHUNK_SIZE-1) * (scale_range-1)` — orders of magnitude larger than `[0, 65535]`. [3](#0-2) 

**`verify_chunking` does NOT bound plaintext chunks to `[0, 65535]` (nizk_chunking.rs lines 327–443):**

The only scalar bound checked is on `z_s` values (a linear combination of chunks and blinding randomness), not on the chunks themselves. The sigma protocol proves *knowledge* of the chunks, not that they lie in the honest range. [4](#0-3) 

**`verify_dealing` calls `verify_zk_proofs` → `verify_chunking` + `verify_sharing` (dealing.rs lines 262–270):**

Neither proof bounds chunks to `[0, 65535]`. A dealing with out-of-range chunks passes all checks. [5](#0-4) 

**Proof the attack path is concrete — test `should_decrypt_correctly_for_cheating_dealer` (tests/forward_secure.rs lines 132–204):**

The test explicitly creates chunks with `sij[cheating_i][cheating_j] *= delta` (delta 2–10), asserts `> CHUNK_MAX`, confirms `verify_ciphertext_integrity` passes, and confirms `dec_chunks` succeeds via `CheatingDealerDlogSolver`. This is the exact attack path. [6](#0-5) 

**`verify_zk_proofs` in `encryption.rs` (lines 337–447):**

`verify_ciphertext_integrity`, `verify_chunking`, and `verify_sharing` are all called and all pass for out-of-range chunks. [7](#0-6) 

---

### Impact Explanation

Every honest receiver node calls `dec_chunks` for every dealing in the transcript during `load_transcript`. A single Byzantine dealer's dealing triggers `CheatingDealerDlogSolver::new(n, m)` on every receiver simultaneously:

- **Memory:** up to 2 GiB allocated per invocation (per cheating dealing, per receiver).
- **CPU:** BSGS online phase iterates up to `scale_range = 2^CHALLENGE_BITS = 256` times per chunk, each requiring a full giant-step walk. The code comment explicitly states: *"It may take hours to brute force a cheater's discrete log."* [8](#0-7) 

With `NUM_CHUNKS = 16` chunks per dealing, all 16 BSGS searches run sequentially. If multiple Byzantine dealers collude (each below the threshold), the cost multiplies. The result is OOM or multi-hour stall on all subnet replicas simultaneously — subnet availability loss.

---

### Likelihood Explanation

The attacker must be an authorized dealer node (a subnet member). This is a protocol peer operating below the consensus fault threshold, which is explicitly within the stated attack surface. The attack requires:

1. Controlling one authorized dealer node.
2. Crafting a dealing with out-of-range chunks and valid NIZK proofs (demonstrated by the existing test).
3. Having the dealing included in the transcript alongside honest dealings.

No threshold majority is required. The `PlaintextChunks::new_unchecked` API and the existing test confirm this is straightforward to execute. [9](#0-8) 

---

### Recommendation

1. **Add a range check in `verify_chunking` or `verify_dealing`:** Reject any dealing where the NIZK witness implies chunks outside `[CHUNK_MIN..CHUNK_MAX]`. This requires either a range proof in the NIZK or a post-decryption check before including a dealing in the transcript.

2. **Add a resource cap to `CheatingDealerDlogSolver`:** Impose a wall-clock or iteration timeout so that a cheating dealer cannot stall a replica indefinitely. Return `DecErr::InvalidChunk` on timeout rather than running for hours.

3. **Rate-limit or quarantine cheating-dealer dealings:** If `HonestDealerDlogLookupTable::solve_several()` returns `None`, log and reject the dealing rather than attempting the expensive BSGS recovery during `load_transcript`.

---

### Proof of Concept

The existing test at `rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs:132` (`should_decrypt_correctly_for_cheating_dealer`) already demonstrates the full attack path: [10](#0-9) 

A Byzantine dealer extends this by:
1. Calling `PlaintextChunks::new_unchecked` with chunks where `chunk > CHUNK_MAX` (e.g., `0x8000 * 2 = 131072`).
2. Calling `enc_chunks` to produce a valid ciphertext.
3. Calling `prove_chunking` to produce a valid NIZK proof (the sigma protocol does not range-check chunks).
4. Submitting the dealing — `verify_dealing` passes.
5. The dealing enters the transcript; every honest receiver's `load_transcript` → `dec_chunks` → `CheatingDealerDlogSolver::new(n, m)` → 2 GiB allocation + hours of BSGS.

The `scripts/cost_estimator.py` confirms the designers modeled `fs_decryption_worst_cost` but did not cap it: [11](#0-10)

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/chunking.rs (L17-20)
```rust
pub const CHUNK_MIN: Chunk = 0;

/// The largest value that a chunk can take
pub const CHUNK_MAX: Chunk = CHUNK_MIN + (CHUNK_SIZE as Chunk) - 1;
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/chunking.rs (L62-67)
```rust
    /// Create a PlaintextChunks without checking that the chunking is valid
    ///
    /// This should only be used for testing
    pub fn new_unchecked(chunks: [Chunk; NUM_CHUNKS]) -> Self {
        Self { chunks }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L820-834)
```rust
    let dlogs = {
        let mut dlogs = linear_search.solve_several(&powers);

        if dlogs.iter().any(|x| x.is_none()) {
            // Cheating dealer case
            let cheating_solver = CheatingDealerDlogSolver::new(n, m);

            for i in 0..dlogs.len() {
                if dlogs[i].is_none() {
                    // TODO(CRP-2550) All BSGS could be run in parallel
                    // It may take hours to brute force a cheater's discrete log.
                    dlogs[i] = cheating_solver.solve(&powers[i]);
                }
            }
        }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L339-366)
```rust
impl CheatingDealerDlogSolver {
    const MAX_TABLE_MBYTES: usize = 2 * 1024; // 2 GiB

    // We limit the maximum table size when compiling without optimizations
    // since otherwise the table becomes so expensive to compute that bazel
    // will fail the test with timeouts.
    const LARGEST_TABLE_MUL: usize = if cfg!(debug_assertions) { 2 } else { 20 };

    pub fn new(n: usize, m: usize) -> Self {
        let scale_range = 1 << CHALLENGE_BITS;
        let ss = n * m * (CHUNK_SIZE - 1) * (scale_range - 1);
        let zz = 2 * NUM_ZK_REPETITIONS * ss;

        let bsgs_lo = 1 - zz as isize;
        let bsgs_range = 2 * zz - 1;

        let baby_giant = BabyStepGiantStep::new(
            Gt::generator(),
            bsgs_lo,
            bsgs_range,
            Self::MAX_TABLE_MBYTES,
            Self::LARGEST_TABLE_MUL,
        );
        Self {
            baby_giant,
            scale_range,
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_chunking.rs (L343-351)
```rust
    let ss = n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK;
    let zz = 2 * NUM_ZK_REPETITIONS * ss;
    let zz_big = Scalar::from_usize(zz);

    for z_sk in nizk.z_s.iter() {
        if z_sk >= &zz_big {
            return Err(ZkProofChunkingError::InvalidProof);
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/dealing.rs (L262-271)
```rust
    verify_zk_proofs(
        epoch,
        receiver_keys,
        &dealing.public_coefficients,
        &dealing.ciphertexts,
        &dealing.zk_proof_decryptability,
        &dealing.zk_proof_correct_sharing,
        &dealer_index.to_be_bytes(),
    )?;
    Ok(())
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs (L132-204)
```rust
#[test]
fn should_decrypt_correctly_for_cheating_dealer() {
    let epoch = Epoch::from(0);
    let rng = &mut reproducible_rng();
    let associated_data = rng.r#gen::<[u8; 10]>();

    let sys = SysParam::global();
    let key_gen_assoc_data = rng.r#gen::<[u8; 32]>();

    let nodes = 3;

    let mut keys = Vec::new();
    for _ in 0..nodes {
        let key_pair = kgen(&key_gen_assoc_data, sys, rng);
        keys.push(key_pair);
    }
    let pks: Vec<_> = keys.iter().map(|key| key.0.public_key().clone()).collect();

    let mut sij = {
        let mut sij = Vec::with_capacity(nodes);

        for _ in 0..nodes {
            let mut chunks = [0; NUM_CHUNKS];

            for i in 0..NUM_CHUNKS {
                // ensure that multiplying by delta pushes us out of Chunk range
                let chunk = (0x8000 | rng.r#gen::<u16>()) as isize;
                chunks[i] = chunk;
            }
            // this ensures that chunks is the encoding of a scalar less
            // than the group order:
            chunks[0] %= 0x73ee;

            sij.push(chunks);
        }

        sij
    };

    let cheating_i = rng.r#gen::<usize>() % nodes;
    let cheating_j = std::cmp::max(1, rng.r#gen::<usize>() % NUM_CHUNKS);

    let delta = (2 + rng.r#gen::<usize>() % 10) as isize;
    sij[cheating_i][cheating_j] *= delta; // doesn't overflow as delta is small and isize >> u16

    // however the new sij *is* larger than the maximum "legal" chunk
    assert!(sij[cheating_i][cheating_j] > CHUNK_MAX);

    let cheating_chunks = sij
        .iter()
        .map(|c| PlaintextChunks::new_unchecked(*c))
        .collect::<Vec<_>>();

    let pks_and_chunks = pks
        .iter()
        .cloned()
        .zip(cheating_chunks.iter().cloned())
        .collect::<Vec<_>>();

    let (crsz, _witness) = enc_chunks(&pks_and_chunks, epoch, &associated_data, sys, rng);

    // still a valid ciphertext
    assert!(verify_ciphertext_integrity(&crsz, epoch, &associated_data, sys).is_ok());

    for i in 0..keys.len() {
        let secret_key = &keys[i].1;
        let out = dec_chunks(secret_key, i, &crsz, epoch, &associated_data);
        assert_eq!(
            out.unwrap(),
            cheating_chunks[i].recombine_to_scalar(),
            "Message decrypted wrongly"
        );
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/encryption.rs (L372-404)
```rust
    crypto::verify_ciphertext_integrity(
        &ciphertext,
        epoch,
        associated_data,
        crypto::SysParam::global(),
    )
    .map_err(|_| {
        CspDkgVerifyDealingError::InvalidDealingError(InvalidArgumentError {
            message: "Ciphertext integrity check failed".to_string(),
        })
    })?;

    let chunking_proof = crypto::ProofChunking::deserialize(chunking_proof).ok_or_else(|| {
        CspDkgVerifyDealingError::MalformedDealingError(InvalidArgumentError {
            message: "Could not parse proof of correct encryption".to_string(),
        })
    })?;

    // Verify proof
    crypto::verify_chunking(
        &crypto::ChunkingInstance::new(
            public_keys.clone(),
            ciphertext.ciphertext_chunks().to_vec(),
            ciphertext.randomizers_r().clone(),
        ),
        &chunking_proof,
    )
    .map_err(|_| {
        let error = InvalidArgumentError {
            message: "Invalid chunking proof".to_string(),
        };
        CspDkgVerifyDealingError::InvalidDealingError(error)
    })?;
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/scripts/cost_estimator.py (L363-365)
```python
fs_decryption_usual_cost = number_of_chunks * (cost(gt, pair4) + cost(gt, search16))
fs_decryption_worst_cost = fs_decryption_usual_cost + cheating_dealer_setup_cost + number_of_chunks*cheating_dealer_search_cost
"""
```
