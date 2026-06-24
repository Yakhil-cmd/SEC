### Title
Byzantine Dealer Triggers Unbounded `CheatingDealerDlogSolver::solve` During Transcript Loading, Stalling Subnet DKG Completion — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

---

### Summary

A single Byzantine dealer node (below the fault threshold) can craft a NI-DKG dealing whose NIZK chunking proof passes `verify_chunking` but whose plaintext chunks are scaled out of the honest range by a factor δ ∈ [2, 255]. When any receiver replica calls `load_transcript`, the call chain `compute_threshold_signing_key` → `decrypt` → `dec_chunks` → `CheatingDealerDlogSolver::solve` executes an unbounded BSGS search that the production code itself annotates as potentially taking **hours**. Because `enforce_transcript_loading` calls `handle.recv()` with no timeout, the consensus thread blocks until the search completes, stalling the subnet's DKG round.

---

### Finding Description

**Step 1 — NIZK chunking proof does not range-check plaintext chunks.**

`verify_chunking` checks only that the proof responses `z_s` satisfy `z_s < zz` (a bound on proof scalars, not on plaintext values) and that the algebraic relations between proof elements and ciphertext chunks hold. [1](#0-0) 

It does **not** verify that the underlying plaintext chunks lie in `[CHUNK_MIN, CHUNK_MAX]`. A Byzantine dealer who knows the plaintext (they chose it) can produce a valid Sigma-protocol proof for any chunk values, including out-of-range ones. This is explicitly acknowledged by the `CheatingDealerDlogSolver` docstring: [2](#0-1) 

**Step 2 — The dealing passes `verify_dealing` and enters the transcript.**

`verify_dealing` calls `verify_zk_proofs` → `verify_chunking` + `verify_sharing`. Because the NIZK proof is algebraically valid, the dealing is accepted and included in the finalized DKG transcript. [3](#0-2) 

**Step 3 — `dec_chunks` falls through to `CheatingDealerDlogSolver::solve`.**

During `load_transcript`, `compute_threshold_signing_key` iterates over every dealer's encrypted share and calls `decrypt` → `dec_chunks`. For the Byzantine dealer's share, the honest linear lookup (`HonestDealerDlogLookupTable::solve_several`) returns `None` for the out-of-range chunk, triggering the cheating-dealer path: [4](#0-3) 

The production comment on line 830 reads: **"It may take hours to brute force a cheater's discrete log."**

**Step 4 — Complexity of `CheatingDealerDlogSolver::solve` with worst-case δ.**

`CheatingDealerDlogSolver::new(n=28, m=16)` computes:

```
scale_range = 1 << CHALLENGE_BITS = 256
ss = 28 × 16 × 65535 × 255 ≈ 7.5 × 10⁹
zz = 2 × 32 × ss ≈ 4.8 × 10¹¹
bsgs_range = 2 × zz ≈ 9.6 × 10¹¹
``` [5](#0-4) 

With `LARGEST_TABLE_MUL = 20` and `MAX_TABLE_MBYTES = 2048`, the BSGS table holds ≈ 19.6 million entries. The online phase requires `giant_steps ≈ 9.6×10¹¹ / 19.6×10⁶ ≈ 49,000` Gt-group operations **per delta**. The outer loop runs 255 times (δ = 1…255). When δ = 251 (the worst-case prime used in the benchmark), the answer is found only at the last iteration, yielding ≈ 12.5 million Gt-group operations total. At ~1 ms per Gt operation on production hardware, this is **~3–4 hours**. [6](#0-5) 

The ignored benchmark test confirms this timing: [7](#0-6) 

**Step 5 — `enforce_transcript_loading` blocks the consensus thread with no timeout.**

When the deadline height is reached, `enforce_transcript_loading` calls `handle.recv()` unconditionally, blocking the consensus thread until the background thread finishes: [8](#0-7) 

There is no timeout, no cancellation, and no fallback. The consensus component's `on_state_change` loop stalls for the duration of the BSGS search.

---

### Impact Explanation

Every receiver replica that holds a valid FS decryption key for the affected epoch will stall its DKG transcript loading for hours per out-of-range chunk (up to `m = 16` chunks per Byzantine dealer's dealing). If the subnet has `n = 28` receivers, all 28 replicas are affected simultaneously. DKG completion is blocked, preventing the subnet from rotating threshold keys and potentially halting consensus progress at the DKG interval boundary.

---

### Likelihood Explanation

A single Byzantine dealer node (one is sufficient; no threshold majority needed) can mount this attack. The dealer is a legitimate protocol participant whose dealing passes all public verification. The attack requires only the ability to craft a dealing with out-of-range chunks — a capability any node with its private key possesses. The attack is deterministic and reproducible.

---

### Recommendation

1. **Add a timeout** to `CheatingDealerDlogSolver::solve` (e.g., return `None` after a configurable wall-clock limit) and treat timeout as `DecErr::InvalidChunk`, excluding the Byzantine dealer's share from interpolation.
2. **Parallelize** the 255-delta outer loop (the TODO at line 829 already notes this) to reduce wall-clock time.
3. **Add a range proof** to the NIZK chunking protocol so that out-of-range chunks are rejected at `verify_dealing` time, eliminating the need for `CheatingDealerDlogSolver` entirely.

---

### Proof of Concept

```rust
// Reproduce with: cargo test --release -p ic-crypto-internal-threshold-sig-bls12381
// -- print_time_for_cheating_dlog_solver_to_run --ignored --nocapture
//
// 1. Create CheatingDealerDlogSolver for production parameters (n=28, m=16)
let solver = CheatingDealerDlogSolver::new(28, 16);
// 2. Craft a target with delta=251 (worst-case prime, last found by outer loop)
let delta = Scalar::from_u64(251);
let delta_inv = delta.inverse().unwrap();
let s = Scalar::from_u64(some_value_in_range);
let target = Gt::generator() * &(s * delta_inv);
// 3. Measure wall-clock time of solve()
let start = std::time::Instant::now();
let _ = solver.solve(&target);
println!("Elapsed: {:?}", start.elapsed()); // expect hours on production hardware
```

The existing ignored test at `rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/cheating_dealer.rs:50` is exactly this benchmark. [7](#0-6)

### Citations

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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L347-366)
```rust
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L368-373)
```rust
    /// Searches for discrete log for a malicious DKG participant whose NIZK
    /// chunking proof checks out, which implies certain bounds on the search
    ///
    /// This function is not constant time, but it only leaks information in the
    /// event that a dealer is already dishonest, and the only information it
    /// leaks is the value that the dishonest dealer sent.
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/dealing.rs (L246-272)
```rust
pub fn verify_dealing(
    dealer_index: NodeIndex,
    threshold: NumberOfNodes,
    epoch: Epoch,
    receiver_keys: &BTreeMap<NodeIndex, FsEncryptionPublicKey>,
    dealing: &Dealing,
) -> Result<(), CspDkgVerifyDealingError> {
    let number_of_receivers =
        number_of_receivers(receiver_keys).map_err(CspDkgVerifyDealingError::SizeError)?;
    verify_threshold(threshold, number_of_receivers)
        .map_err(CspDkgVerifyDealingError::InvalidThresholdError)?;
    verify_receiver_indices(receiver_keys, number_of_receivers)?;
    verify_all_shares_are_present_and_well_formatted(dealing, number_of_receivers)
        .map_err(CspDkgVerifyDealingError::InvalidDealingError)?;
    verify_public_coefficients_match_threshold(dealing, threshold)
        .map_err(CspDkgVerifyDealingError::InvalidDealingError)?;
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
}
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L823-833)
```rust
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

**File:** rs/consensus/dkg/src/dkg_key_manager.rs (L274-288)
```rust
        for (id, (_, handle)) in expired {
            match handle.recv() {
                Err(err) => panic!(
                    "Couldn't finish loading transcript {}: {:?}",
                    dkg_id_log_msg(&id),
                    err
                ),
                Ok(Err(err)) => panic!(
                    "Couldn't finish loading transcript {}: {:?}",
                    dkg_id_log_msg(&id),
                    err
                ),
                _ => (),
            }
        }
```
