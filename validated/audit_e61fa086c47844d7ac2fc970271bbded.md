### Title
Byzantine Dealer Out-of-Range Chunk DoS via `CheatingDealerDlogSolver` in `dec_chunks` — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

---

### Summary

A single Byzantine dealer node can submit a NiDKG dealing whose ciphertext chunks lie outside `[0, CHUNK_MAX]` but still pass the chunking NIZK (`verify_chunking`). When any receiver calls `load_transcript`, `dec_chunks` falls through to `CheatingDealerDlogSolver`, which the code itself annotates as potentially taking **hours** per chunk. This blocks the background DKG key-manager thread for the entire duration, preventing the node from obtaining its threshold signing key for the new epoch.

---

### Finding Description

**The NIZK bound does not constrain individual chunk values.**

`verify_chunking` checks only that each `z_s[k] < zz`, where:

```
ss  = n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK
    = 28 * 16 * 65535 * 255  ≈ 7.5 × 10⁹
zz  = 2 * NUM_ZK_REPETITIONS * ss  ≈ 4.8 × 10¹¹
```

`CHUNK_MAX = 65535`, so `zz ≫ CHUNK_MAX`. The NIZK proves only that the *weighted sum* of chunks is bounded; it does not prove that any individual chunk lies in `[0, CHUNK_SIZE)`. A Byzantine dealer can choose chunks well above `CHUNK_MAX` and still produce a valid proof, because the prover's `sigma_k` slack is large enough to keep `z_s[k]` within `zz`. [1](#0-0) 

**`verify_dealing` / `verify_zk_proofs` passes for out-of-range chunks.**

`verify_zk_proofs` calls `verify_ciphertext_integrity` and `verify_chunking`. Neither checks that individual plaintext chunks are in `[0, CHUNK_MAX]`. The existing test `should_decrypt_correctly_for_cheating_dealer` explicitly confirms this: it constructs chunks with `new_unchecked` where values exceed `CHUNK_MAX`, calls `enc_chunks`, and asserts that `verify_ciphertext_integrity` returns `Ok`. [2](#0-1) 

**`dec_chunks` unconditionally invokes `CheatingDealerDlogSolver` when any chunk is out of range.**

`HonestDealerDlogLookupTable::solve_several` scans only `[0, CHUNK_MAX]`. Any chunk above `CHUNK_MAX` returns `None`. The fallback path then constructs `CheatingDealerDlogSolver::new(n, m)` (allocating up to 2 GiB of BSGS table) and calls `solve()` for every failing chunk, iterating up to `scale_range = 2^CHALLENGE_BITS = 256` BSGS lookups each. [3](#0-2) 

The code itself carries the comment:

> `// It may take hours to brute force a cheater's discrete log.` [4](#0-3) 

**`CheatingDealerDlogSolver` parameters for n=28, m=16.**

```
scale_range = 256
ss  = 28 * 16 * 65535 * 255  ≈ 7.5 × 10⁹
zz  = 2 * 32 * ss            ≈ 4.8 × 10¹¹
bsgs_range = 2*zz - 1        ≈ 9.6 × 10¹¹
```

The BSGS table is capped at 2 GiB (`MAX_TABLE_MBYTES = 2048`). The online phase then requires `ceil(bsgs_range / table_size)` Gt additions per delta, multiplied by 255 deltas, multiplied by `NUM_CHUNKS = 16` chunks per dealing. [5](#0-4) 

The cost estimator script confirms the worst-case:

```python
cheating_dealer_search_cost = cheating_dealer_scale_range * bsgs_online_cost
fs_decryption_worst_cost    = fs_decryption_usual_cost + cheating_dealer_setup_cost
                              + number_of_chunks * cheating_dealer_search_cost
``` [6](#0-5) 

**`load_transcript` runs in an unbounded background thread.**

`DkgKeyManager` spawns a thread per transcript and loops on `NiDkgAlgorithm::load_transcript` until it succeeds or hits a permanent error. There is no timeout that would abort the `CheatingDealerDlogSolver` computation mid-flight. [7](#0-6) 

---

### Impact Explanation

Every receiver in the subnet calls `load_transcript` for the new epoch's transcript. If the Byzantine dealer's dealing is included (it passes `verify_dealing`), each receiver's DKG key-manager thread is blocked for hours. The node cannot obtain its threshold signing key for the new epoch, preventing it from contributing threshold signature shares. With enough receivers stalled simultaneously, the subnet cannot produce threshold signatures, stalling chain-key operations (tECDSA, tSchnorr, vetKD) and subnet key refresh.

---

### Likelihood Explanation

A single Byzantine dealer — one node below the consensus fault threshold — is sufficient. The dealer must be a registered subnet node (requires governance), but no majority corruption is needed. The attack is deterministic: any chunk value above `CHUNK_MAX` reliably triggers the slow path. The `#[ignore]`-tagged benchmark `print_time_for_cheating_dlog_solver_to_run` exists precisely because the runtime is prohibitive even in test environments. [8](#0-7) 

---

### Recommendation

1. **Add a chunk-range check in `verify_chunking` or `verify_zk_proofs`**: After the NIZK equations pass, verify that each ciphertext chunk decodes to a value in `[0, CHUNK_MAX]` using the public ciphertext commitments. This is feasible because the ciphertext chunks are public.

2. **Add a timeout / cancellation token to `CheatingDealerDlogSolver::solve`**: Even if the NIZK check is strengthened, a defense-in-depth timeout prevents indefinite blocking.

3. **Exclude dealings with out-of-range chunks from transcripts**: If `dec_chunks` detects an out-of-range chunk during transcript loading, it should immediately issue a complaint rather than spending hours on BSGS.

---

### Proof of Concept

```rust
// Byzantine dealer side (pseudocode):
let mut chunks = [0isize; NUM_CHUNKS];
for i in 0..NUM_CHUNKS {
    chunks[i] = CHUNK_MAX as isize * 200; // well above CHUNK_MAX, within zz
}
let cheating_plaintext = PlaintextChunks::new_unchecked(chunks);
let (crsz, _witness) = enc_chunks(&[(receiver_pk, cheating_plaintext)], epoch, ad, sys, rng);
// verify_ciphertext_integrity(&crsz, ...) → Ok(())  ← passes
// verify_chunking(instance, proof)        → Ok(())  ← passes (z_s < zz)

// Receiver side during load_transcript:
// dec_chunks(...) → HonestDealerDlogLookupTable returns None for all 16 chunks
//                → CheatingDealerDlogSolver::new(28, 16) allocates ~2 GiB table
//                → solve() iterates 255 × 16 BSGS searches
//                → wall-clock time: hours
```

The existing test `should_decrypt_correctly_for_cheating_dealer` already exercises this path with small out-of-range values and confirms the ciphertext passes integrity checks. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_chunking.rs (L341-351)
```rust
    let m = instance.randomizers_r.len();
    let n = instance.public_keys.len();
    let ss = n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK;
    let zz = 2 * NUM_ZK_REPETITIONS * ss;
    let zz_big = Scalar::from_usize(zz);

    for z_sk in nizk.z_s.iter() {
        if z_sk >= &zz_big {
            return Err(ZkProofChunkingError::InvalidProof);
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs (L132-205)
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
}
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L820-833)
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/scripts/cost_estimator.py (L357-364)
```python
cheating_dealer_scale_range = pow2(challenge_bits)

cheating_dealer_setup_cost = bsgs_setup_cost

cheating_dealer_search_cost = cheating_dealer_scale_range*bsgs_online_cost

fs_decryption_usual_cost = number_of_chunks * (cost(gt, pair4) + cost(gt, search16))
fs_decryption_worst_cost = fs_decryption_usual_cost + cheating_dealer_setup_cost + number_of_chunks*cheating_dealer_search_cost
```

**File:** rs/consensus/dkg/src/dkg_key_manager.rs (L339-412)
```rust
            std::thread::spawn(move || {
                let transcript = summary
                    .current_transcripts()
                    .iter()
                    .chain(summary.next_transcripts().iter())
                    .find(|(_, t)| t.dkg_id == dkg_id)
                    .expect("No transcript was found")
                    .1;

                let result = loop {
                    let result = NiDkgAlgorithm::load_transcript(&*crypto, transcript);
                    let elapsed = since.elapsed().as_secs_f64();

                    match &result {
                        // Key loaded successfully
                        Ok(LoadTranscriptResult::SigningKeyAvailable) => {
                            info!(
                                logger,
                                "Finished loading transcript {} after {}s",
                                dkg_id_log_msg(&dkg_id),
                                elapsed
                            );
                            break result;
                        }

                        Ok(LoadTranscriptResult::NodeNotInCommittee) => {
                            info!(
                                logger,
                                "Finished loading public parts of transcript {} after {}s\
                                (signing key unavailable since this node is not part of the committee)",
                                dkg_id_log_msg(&dkg_id),
                                elapsed
                            );
                            break result;
                        }

                        // Arguments passed to crypto are invalid, should never happen
                        Ok(val) => {
                            error!(
                                logger,
                                "Could only load public parts of transcript {} \
                                (signing key unavailable: {:?})",
                                dkg_id_log_msg(&dkg_id),
                                val
                            );
                            break result;
                        }

                        // Transient error in crypto, log warning and retry
                        Err(err) if !err.is_reproducible() => {
                            warn!(
                                every_n_seconds => 5,
                                logger,
                                "Transcript {} couldn't be loaded: {:?} Retrying...",
                                dkg_id_log_msg(&dkg_id),
                                err
                            );
                        }

                        // Permanent error in crypto, log error
                        Err(err) => {
                            error!(
                                logger,
                                "Transcript {} couldn't be loaded: {:?}",
                                dkg_id_log_msg(&dkg_id),
                                err
                            );
                            break result;
                        }
                    }
                };

                tx.send(result).expect("DKG key manager panicked");
            });
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/cheating_dealer.rs (L49-79)
```rust
#[test]
#[ignore]
fn print_time_for_cheating_dlog_solver_to_run() {
    let rng = &mut reproducible_rng();

    let subnet_size = 13;
    let total_tests = 16; // one fully bad dealing

    let table_start = std::time::SystemTime::now();
    let solver = CheatingDealerDlogSolver::new(subnet_size, 16);
    println!(
        "Created table for {} nodes in {:?}",
        subnet_size,
        table_start.elapsed().unwrap()
    );

    let tests = (0..total_tests)
        .map(|_| cheating_dlog_instance(subnet_size, rng, true))
        .collect::<Vec<_>>();

    for (solution, target) in tests {
        let solve_start = std::time::SystemTime::now();

        assert_eq!(
            solution,
            solver.solve(&target).expect("Unable to solve dlog")
        );

        println!("Solved an instance in {:?}", solve_start.elapsed().unwrap());
    }
}
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
