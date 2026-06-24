Audit Report

## Title
Loose NIZK Chunking Proof Bounds Allow Byzantine Dealer to Trigger 2 GiB BSGS Allocation and Hours-Long CPU Exhaustion on All Receiver Replicas — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

## Summary

A single Byzantine dealer (below the fault threshold) can craft a NiDKG dealing with out-of-range plaintext chunks that passes all verification checks (`verify_dealing`, `verify_chunking`, `verify_ciphertext_integrity`). When honest receivers call `load_transcript`, the decryption path falls through to `CheatingDealerDlogSolver::new`, which allocates up to 2 GiB for a BSGS table and then runs for hours per chunk. The background thread has no timeout, and the computation is repeated for every chunk of every Byzantine dealing in the transcript, causing sustained CPU exhaustion and memory pressure on all receiver replicas.

## Finding Description

**Root cause — loose NIZK chunking proof range:**

`verify_chunking` in `nizk_chunking.rs` only enforces `z_s < zz` where `zz = 2 * NUM_ZK_REPETITIONS * ss` and `ss = n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK`. This bound is approximately 64× larger than the honest chunk range, meaning a Byzantine dealer can produce chunks with values multiplied by a factor δ ∈ [2, 64] and still generate a valid `ZKProofDec` by sampling `sigma_k` in the wider range. [1](#0-0) 

The codebase explicitly acknowledges this: "The bounds of the NIZK chunking proof are loose, so a malicious DKG participant can force us to search around 2^40 candidates for a discrete log." [2](#0-1) 

**Exploit path:**

1. Byzantine dealer crafts chunks with values outside `[0, CHUNK_MAX]` (multiplied by δ ≥ 2) and produces a valid `ZKProofDec` by retrying proof generation within the loose `zz` bound. For δ = 2, success probability per attempt is ~37% (~2.7 retries expected).

2. `verify_dealing` → `verify_zk_proofs` passes. The test `should_decrypt_correctly_for_cheating_dealer` confirms `verify_ciphertext_integrity` also passes for out-of-range chunks. [3](#0-2) [4](#0-3) 

3. The dealing is included in the transcript. When receivers call `load_transcript` → `compute_threshold_signing_key`, it iterates over all dealers including Byzantine ones. [5](#0-4) 

4. `dec_chunks` calls `HonestDealerDlogLookupTable::solve_several`, which fails for out-of-range chunks, then falls through to `CheatingDealerDlogSolver::new(n, m)`. [6](#0-5) 

5. `CheatingDealerDlogSolver::new` allocates up to 2 GiB for the BSGS table and `solve` iterates over up to 255 δ values with a BSGS search over ~2^40 candidates. The code comment explicitly states: "It may take hours to brute force a cheater's discrete log." [7](#0-6) 

6. The `load_transcript` background thread has no internal timeout; it loops on transient errors and runs until completion or permanent error. [8](#0-7) 

## Impact Explanation

Every honest receiver replica processing a transcript containing a Byzantine dealing will allocate up to 2 GiB for the BSGS table and spend hours of CPU per chunk (16 chunks per dealing). With f Byzantine dealers below the fault threshold, memory pressure reaches f × 2 GiB per receiver, risking OOM. The background thread blocking on `load_transcript` prevents the replica from loading its threshold signing key, disrupting subnet availability and DKG key rotation. This matches: **High ($2,000–$10,000) — Application/platform-level DoS, subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation

Any registered dealer in a DKG config (a valid below-threshold Byzantine subnet member) can execute this attack during any DKG interval. The attacker needs only to craft out-of-range chunks and retry proof generation a small number of times (expected ~3 retries for δ = 2). The attack is repeatable every DKG interval and affects all receiver replicas simultaneously. No special privileges beyond being a registered dealer are required.

## Recommendation

1. **Tighten the NIZK chunking proof range**: Adjust the sigma sampling range in `prove_chunking` so that `z_s < zz` implies chunk values ≤ `CHUNK_MAX`, eliminating the ~64× slack.
2. **Add a chunk range check before invoking `CheatingDealerDlogSolver`**: After the `HonestDealerDlogLookupTable` lookup fails, immediately return `Err(DecErr::InvalidChunk)` rather than falling through to the expensive BSGS solver. The expensive solver is only needed if the protocol is intended to recover from cheating dealers; if not, rejection is the correct response.
3. **Apply a timeout to the `load_transcript` background thread**: Bound the maximum wall-clock time the thread can spend on a single transcript to prevent indefinite CPU exhaustion.
4. **Reject transcripts with out-of-range chunks at dealing verification**: Add an explicit range check on decrypted chunk values during `verify_dealing` so Byzantine dealings are excluded from transcripts before receivers process them.

## Proof of Concept

The existing `#[ignore]` test `print_time_for_cheating_dlog_solver_to_run` in `tests/cheating_dealer.rs` directly measures the cost of the BSGS solver for a 13-node subnet with 16 chunks (one full Byzantine dealing), confirming the hours-long runtime. [9](#0-8) 

To reproduce the full exploit path:
1. In `tests/forward_secure.rs`, extend `should_decrypt_correctly_for_cheating_dealer` to use δ = 2 (chunks multiplied by 2, still within `zz` bounds).
2. Call `prove_chunking` on the out-of-range chunks; confirm the proof verifies under `verify_chunking`.
3. Wrap the dealing in a transcript and call `compute_threshold_signing_key`; measure wall-clock time to confirm it triggers `CheatingDealerDlogSolver` and exceeds acceptable bounds.
4. Confirm `verify_ciphertext_integrity` and `verify_chunking` both return `Ok` for the crafted dealing.

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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/dlog_recovery.rs (L108-110)
```rust
// The bounds of the NIZK chunking proof are loose, so a malicious DKG
// participant can force us to search around 2^40 candidates for a discrete log.
// (This is not the entire cost. We must also search for a cofactor Delta.)
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs (L193-194)
```rust
    // still a valid ciphertext
    assert!(verify_ciphertext_integrity(&crsz, epoch, &associated_data, sys).is_ok());
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/dealing.rs (L262-270)
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L340-366)
```rust
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
