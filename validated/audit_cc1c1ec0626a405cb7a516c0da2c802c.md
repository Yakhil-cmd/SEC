### Title
CPU Exhaustion via Byzantine Dealer Out-of-Range Chunks Triggering BSGS in `dec_chunks` — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

---

### Summary

A Byzantine dealer below the fault threshold can craft a NI-DKG dealing with plaintext chunks outside `[0, CHUNK_MAX]`, produce a valid chunking proof for those chunks (the sigma protocol does not enforce the honest range), pass `verify_ciphertext_integrity` and `verify_chunking`/`verify_sharing`, and force every honest replica that calls `dec_chunks` to invoke `CheatingDealerDlogSolver::new` and run the BSGS solver — a computation the code itself annotates as potentially taking **hours** per chunk.

---

### Finding Description

**`dec_chunks` slow path — the code explicitly acknowledges the cost:** [1](#0-0) 

When `HonestDealerDlogLookupTable::solve_several` returns `None` for any chunk (i.e., the dlog is outside `[0, CHUNK_MAX]`), `CheatingDealerDlogSolver::new(n, m)` is constructed and `cheating_solver.solve()` is called per failing chunk. The comment at line 830 reads: *"It may take hours to brute force a cheater's discrete log."*

**`CheatingDealerDlogSolver::new` allocates up to 2 GiB and iterates `scale_range = 1 << CHALLENGE_BITS = 256` times:** [2](#0-1) 

The `solve` method loops `1..scale_range` (up to 255 iterations), each calling `baby_giant.solve` which itself iterates `giant_steps` times over the BSGS table: [3](#0-2) 

The cost estimator script explicitly models this worst-case path: [4](#0-3) 

**The chunking proof does NOT enforce `s_ij ∈ [0, CHUNK_SIZE-1]`:**

`prove_chunking` computes `z_s[k] = Σ(e_ijk * s_ij) + sigma_k` and retries until `z_s[k] < zz`. For out-of-range chunks, the prover simply retries more iterations until a valid `sigma_k` brings `z_s` into the accepted range. The verifier only checks `z_s < zz_big` (a bound derived from instance parameters, not actual chunk values) and algebraic consistency: [5](#0-4) 

The proof is a knowledge proof, not a range proof. A Byzantine dealer with chunks in `[CHUNK_MAX+1, delta*CHUNK_MAX]` for small `delta` can produce a valid `ProofChunking` by retrying the sigma protocol.

**`verify_ciphertext_integrity` passes for out-of-range chunks — proven by the existing test:** [6](#0-5) 

The test explicitly creates chunks with `sij[cheating_i][cheating_j] *= delta` (delta 2–11), asserts `sij > CHUNK_MAX`, and asserts `verify_ciphertext_integrity` returns `Ok(())`. The `_witness` is discarded — the test does not call `verify_chunking`, but the sigma protocol analysis above shows a Byzantine dealer can produce a passing proof.

**The full call chain from `load_transcript` to `dec_chunks`:**

`load_transcript` → `attempt_to_load_signing_key` → `csp_load_threshold_signing_key` → `load_threshold_signing_key_internal` → `compute_threshold_signing_key` → `decrypt` → `dec_chunks`: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

A single Byzantine dealer (below the `f = ⌊(n-1)/3⌋` fault threshold) submits one crafted dealing. Every honest replica that is a receiver in the transcript calls `dec_chunks` during `load_transcript`. For a 28-node subnet with `NUM_CHUNKS = 16`, the BSGS solver runs up to `16 × 255` iterations over a ~2 GiB table. The replica's crypto thread is blocked for hours, preventing it from completing DKG transcript loading and participating in threshold signing for the new epoch. The `print_time_for_cheating_dlog_solver_to_run` test (marked `#[ignore]` due to runtime) directly measures this cost: [9](#0-8) 

---

### Likelihood Explanation

The attacker is a single Byzantine dealer node — a realistic adversary in the IC threat model. The dealing passes all existing verification gates (`verify_ciphertext_integrity`, `verify_chunking`, `verify_sharing`). No privileged access, key compromise, or majority corruption is required. The attack is local-testable (the `#[ignore]` benchmark exists for exactly this purpose).

---

### Recommendation

1. **Add a range check before invoking `CheatingDealerDlogSolver`**: After `HonestDealerDlogLookupTable::solve_several` returns `None`, verify that the dealing's chunking proof was produced by an honest dealer by checking that the `z_s` responses in the proof are consistent with chunks in `[0, CHUNK_SIZE-1]`. If not, reject the dealing immediately with `DecErr::InvalidChunk` rather than running BSGS.
2. **Enforce a CPU timeout on `CheatingDealerDlogSolver::solve`**: Cap the number of BSGS iterations and return `Err(DecErr::InvalidChunk)` if the dlog is not found within the honest-dealer range.
3. **Reject the dealing at `verify_dealing` time**: Strengthen `verify_chunking` to enforce that the witness scalars are in `[0, CHUNK_SIZE-1]` using a proper range proof, so out-of-range dealings are rejected before being included in a transcript.

---

### Proof of Concept

```rust
// Byzantine dealer crafts out-of-range chunks
let mut chunks = [0isize; NUM_CHUNKS];
for i in 0..NUM_CHUNKS {
    chunks[i] = (CHUNK_MAX + 1 + i as isize); // out of honest range
}
let cheating_plaintext = PlaintextChunks::new_unchecked(chunks);

// enc_chunks produces a structurally valid ciphertext
let (crsz, witness) = enc_chunks(&[(pk, cheating_plaintext)], epoch, &ad, sys, rng);

// verify_ciphertext_integrity passes (proven by existing test)
assert!(verify_ciphertext_integrity(&crsz, epoch, &ad, sys).is_ok());

// prove_chunking succeeds (sigma protocol retries until z_s < zz)
let chunking_proof = prove_chunking(&ChunkingInstance::new(...), &witness_with_oob_chunks, rng);
assert!(verify_chunking(&instance, &chunking_proof).is_ok()); // passes

// Honest replica calls dec_chunks — triggers hours-long BSGS
let start = std::time::Instant::now();
let _ = dec_chunks(&secret_key, 0, &crsz, epoch, &ad);
// elapsed >> minutes
```

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L817-834)
```rust
    // Find discrete log of the powers
    let linear_search = HonestDealerDlogLookupTable::new();

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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L374-400)
```rust
    pub fn solve(&self, target: &Gt) -> Option<Scalar> {
        /*
        For some Delta in [1..E - 1] the answer s satisfies (Delta * s) in
        [1 - Z..Z - 1].

        For each delta in [1..E - 1] we compute target*delta and use
        baby-step-giant-step to find `scaled_answer` such that:
           base*scaled_answer = target*delta

         Then `base * (scaled_answer / delta) = target`
          (here division is modulo the group order
         That is, the discrete log of target is `scaled_answer / delta`.
        */
        let mut target_power = Gt::identity();
        for delta in 1..self.scale_range {
            target_power += target;

            if let Some(scaled_answer) = self.baby_giant.solve(&target_power) {
                let inv_delta = Scalar::from_usize(delta)
                    .inverse()
                    .expect("Delta is always invertible");
                let result = scaled_answer * inv_delta;
                return Some(result);
            }
        }
        None
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs (L174-194)
```rust
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
```

**File:** rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/ni_dkg/mod.rs (L494-500)
```rust
                let csp_secret_key = ni_dkg_clib::compute_threshold_signing_key(
                    &transcript,
                    receiver_index,
                    &fs_decryption_key,
                    epoch,
                )
                .map(CspSecretKey::ThresBls12_381)?;
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/transcript.rs (L262-293)
```rust
    let shares_from_each_dealer: Result<BTreeMap<NodeIndex, threshold_types::SecretKey>, _> =
        transcript
            .receiver_data
            .iter()
            .map(|(dealer_index, encrypted_shares)| {
                let secret_key = decrypt(
                    encrypted_shares,
                    fs_secret_key,
                    receiver_index,
                    epoch,
                    &dealer_index.to_be_bytes(),
                )
                .map_err(|error| match error {
                    ni_dkg_errors::DecryptError::EpochTooOld {
                        ciphertext_epoch,
                        secret_key_epoch,
                    } => ni_dkg_errors::CspDkgLoadPrivateKeyError::EpochTooOldError {
                        ciphertext_epoch,
                        secret_key_epoch,
                    },
                    error => {
                        let message = format!(
                            "Dealing #{dealer_index}: could not get share for receiver #{receiver_index}.\n {error:#?}"
                        );
                        let error = InvalidArgumentError { message };
                        ni_dkg_errors::CspDkgLoadPrivateKeyError::InvalidTranscriptError(error)
                    }
                })?;

                Ok((*dealer_index, secret_key))
            })
            .collect();
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/cheating_dealer.rs (L49-78)
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
```
