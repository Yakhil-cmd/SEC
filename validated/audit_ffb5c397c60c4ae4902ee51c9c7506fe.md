Audit Report

## Title
Byzantine Dealer Triggers Unbounded BSGS Search in `CheatingDealerDlogSolver::solve`, Blocking Consensus Thread During DKG Transcript Loading — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

## Summary
A single Byzantine dealer node can craft a NI-DKG dealing whose NIZK chunking proof is algebraically valid but whose plaintext chunks are scaled out of the honest range. When any receiver replica calls `load_transcript`, the `dec_chunks` function falls through to `CheatingDealerDlogSolver::solve`, which the production code itself annotates as potentially taking **hours**. Because `enforce_transcript_loading` calls `handle.recv()` with no timeout when the DKG deadline height is reached, the consensus thread blocks until the search completes, stalling the subnet's DKG round.

## Finding Description

**Step 1 — `verify_chunking` does not range-check plaintext chunks.**

`verify_chunking` in `nizk_chunking.rs` checks only that the proof response scalars `z_s` satisfy `z_sk < zz_big` and that the algebraic Sigma-protocol relations hold. It does **not** verify that the underlying plaintext chunks lie in `[CHUNK_MIN, CHUNK_MAX]`. A Byzantine dealer who chose the plaintext can produce a valid proof for any chunk values, including out-of-range ones scaled by δ ∈ [2, 255]. [1](#0-0) 

**Step 2 — The dealing passes `verify_dealing` and enters the transcript.**

`verify_dealing` calls `verify_zk_proofs` → `verify_chunking` + `verify_sharing`. Because the NIZK proof is algebraically valid, the dealing is accepted and included in the finalized DKG transcript. [2](#0-1) 

**Step 3 — `dec_chunks` falls through to `CheatingDealerDlogSolver::solve`.**

During `load_transcript`, `dec_chunks` first attempts `HonestDealerDlogLookupTable::solve_several`. For the Byzantine dealer's out-of-range chunk, this returns `None`, triggering the cheating-dealer path. The production comment at line 830 reads: **"It may take hours to brute force a cheater's discrete log."** [3](#0-2) 

**Step 4 — `CheatingDealerDlogSolver::solve` is an unbounded outer loop over 255 delta values.**

`CheatingDealerDlogSolver::new(n, m)` computes a BSGS range of `2 * zz - 1` where `zz = 2 * NUM_ZK_REPETITIONS * n * m * (CHUNK_SIZE - 1) * (scale_range - 1)`. For production parameters (n=28, m=16), this is ~9.6×10¹¹. The `solve` method iterates `delta` from 1 to 255, running a full BSGS search for each. With `LARGEST_TABLE_MUL = 20` and `MAX_TABLE_MBYTES = 2048`, the table holds ~19.6M entries, requiring ~49,000 giant steps per delta. At worst case (δ=251), the answer is found only at the last iteration. [4](#0-3) 

The ignored benchmark test confirms this timing concern: [5](#0-4) 

**Step 5 — `enforce_transcript_loading` blocks the consensus thread with no timeout.**

`load_transcripts_from_summary` spawns a background thread per transcript. When the deadline height is reached, `enforce_transcript_loading` calls `handle.recv()` unconditionally — `Receiver::recv()` blocks indefinitely with no timeout, no cancellation, and no fallback. The consensus component's `on_state_change` loop stalls for the full duration of the BSGS search. [6](#0-5) 

## Impact Explanation

This is a **High** severity subnet availability impact. When the DKG deadline height is reached, all receiver replicas simultaneously block their consensus thread for hours per out-of-range chunk (up to `m = 16` chunks per Byzantine dealer's dealing). DKG completion is blocked, preventing threshold key rotation and halting consensus progress at the DKG interval boundary. This matches the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation

A single Byzantine dealer node (one is sufficient; no threshold majority needed) can mount this attack. The dealer is a legitimate protocol participant whose dealing passes all public verification (`verify_dealing`). The attack requires only the ability to craft a dealing with out-of-range chunks — a capability any node with its private FS encryption key possesses. The attack is deterministic, reproducible, and requires no victim mistakes or external compromise.

## Recommendation

1. **Add a timeout** to `CheatingDealerDlogSolver::solve` (e.g., return `None` after a configurable wall-clock limit) and treat timeout as `DecErr::InvalidChunk`, excluding the Byzantine dealer's share from interpolation.
2. **Parallelize** the 255-delta outer loop (the `TODO(CRP-2550)` at line 829 already notes this) to reduce wall-clock time.
3. **Add a range proof** to the NIZK chunking protocol so that out-of-range chunks are rejected at `verify_dealing` time, eliminating the need for `CheatingDealerDlogSolver` entirely.

## Proof of Concept

The existing ignored benchmark test at `rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/cheating_dealer.rs:49` is exactly this benchmark. Run with:

```
cargo test --release -p ic-crypto-internal-threshold-sig-bls12381 \
  -- print_time_for_cheating_dlog_solver_to_run --ignored --nocapture
```

For a full end-to-end PoC: craft a dealing where one receiver's chunk is `G^(s/delta)` for `delta=251` and `s` in the honest range, generate a valid NIZK chunking proof for this dealing (the proof verifies algebraically), submit it through the DKG protocol, and measure the wall-clock time of `load_transcript` on a receiver replica when the DKG deadline height is reached.

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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L347-400)
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

    /// Searches for discrete log for a malicious DKG participant whose NIZK
    /// chunking proof checks out, which implies certain bounds on the search
    ///
    /// This function is not constant time, but it only leaks information in the
    /// event that a dealer is already dishonest, and the only information it
    /// leaks is the value that the dishonest dealer sent.
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
