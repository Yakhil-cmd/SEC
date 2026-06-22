Based on my thorough code analysis, here is my assessment:

---

### Title
Byzantine Dealer DoS via Out-of-Range Chunks Triggering Unbounded BSGS Table Allocation in `dec_chunks` — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

### Summary

A single Byzantine dealer below the fault threshold can craft a dealing with valid NIZK proofs but out-of-range plaintext chunks. When honest nodes call `load_transcript`, `dec_chunks` falls through to `CheatingDealerDlogSolver::new(n, m)`, which allocates up to 2 GiB of BSGS table memory and may stall for hours per dealing per node.

### Finding Description

The attack exploits a gap between what `verify_chunking` (the NIZK proof) guarantees and what `dec_chunks` assumes.

**Step 1 — NIZK proof does not bound chunks to `[CHUNK_MIN, CHUNK_MAX]`.**

`verify_chunking` checks that the `z_s` response scalars satisfy:

```
zz = 2 * NUM_ZK_REPETITIONS * n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK
``` [1](#0-0) 

This bound is orders of magnitude larger than `CHUNK_MAX = 0xFFFF`. The proof proves knowledge of the plaintext chunks but does not constrain individual `s_{i,j}` values to `[CHUNK_MIN, CHUNK_MAX]`. The existence of `CheatingDealerDlogSolver` itself confirms this: it would be unnecessary if the NIZK proof prevented out-of-range chunks.

**Step 2 — `verify_ciphertext_integrity` also passes for out-of-range chunks.**

The test `should_decrypt_correctly_for_cheating_dealer` explicitly confirms this:

```rust
assert!(verify_ciphertext_integrity(&crsz, epoch, &associated_data, sys).is_ok());
``` [2](#0-1) 

`verify_ciphertext_integrity` only checks pairing equations over the group elements; it has no visibility into the integer value of the plaintext chunks. [3](#0-2) 

**Step 3 — `dec_chunks` unconditionally allocates the BSGS table on any out-of-range chunk.**

```rust
if dlogs.iter().any(|x| x.is_none()) {
    // Cheating dealer case
    let cheating_solver = CheatingDealerDlogSolver::new(n, m);
    // It may take hours to brute force a cheater's discrete log.
    ...
}
``` [4](#0-3) 

`CheatingDealerDlogSolver::new` is hardcoded to allow up to 2 GiB:

```rust
const MAX_TABLE_MBYTES: usize = 2 * 1024; // 2 GiB
``` [5](#0-4) 

For a 40-node subnet (n=40, m=16), the BSGS range `2*zz-1` is on the order of hundreds of billions, requiring a table of ~20–26 million entries (~1 GiB) and ~65,000 giant steps per chunk, each requiring a `Gt` group operation and hash lookup.

**Step 4 — `load_transcript` calls `dec_chunks` for every dealing in the transcript.**

The call chain is:

`load_transcript` → `compute_threshold_signing_key` → `decrypt` → `dec_chunks` [6](#0-5) [7](#0-6) 

Every honest node in the subnet calls `load_transcript` when a new DKG summary block is finalized. [8](#0-7) 

### Impact Explanation

- **Memory**: Each node allocates up to 2 GiB for the BSGS table per cheating dealing. With multiple cheating dealers (still below the fault threshold), this multiplies.
- **Time**: The BSGS search "may take hours" (per the code comment). During this time, the node cannot obtain its threshold signing key for the new epoch, preventing it from participating in threshold signing.
- **Scope**: All honest nodes in the subnet are affected simultaneously when loading the same transcript.
- **Persistence**: The stall occurs on every epoch transition that includes the cheating dealing.

### Likelihood Explanation

- Requires only a single Byzantine dealer node (well below the fault threshold).
- The dealing passes all public verification steps (`verify_chunking`, `verify_sharing`, `verify_ciphertext_integrity`).
- The attack is deterministic and reproducible.
- No privileged access, key material, or network-level attack is required.

### Recommendation

1. **Bound check before BSGS**: After `HonestDealerDlogLookupTable::solve_several` returns `None`, verify that the decrypted group element is plausibly within the extended range before invoking `CheatingDealerDlogSolver`. If the element is provably outside the BSGS search range, return `Err(DecErr::InvalidChunk)` immediately.
2. **Reject cheating dealings at verification time**: Extend `verify_dealing` to detect and reject dealings whose NIZK proof witnesses imply out-of-range chunks, so such dealings never enter the transcript.
3. **Cap BSGS computation time**: Add a timeout or iteration limit to `CheatingDealerDlogSolver::solve` so that a single bad dealing cannot stall a node indefinitely.
4. **Rate-limit or sandbox transcript loading**: Run `load_transcript` with memory and CPU limits so that a malicious dealing cannot exhaust node resources.

### Proof of Concept

```rust
// Craft a dealing with valid NIZK proofs but out-of-range chunks
// (mirrors the existing test in forward_secure.rs)
let chunk = (0x8000 | rng.gen::<u16>()) as isize; // > CHUNK_MAX
let delta = 10isize;
sij[cheating_i][cheating_j] = chunk * delta; // >> CHUNK_MAX, still within zz bound

let cheating_chunks = sij.iter().map(|c| PlaintextChunks::new_unchecked(*c)).collect();
let (crsz, _witness) = enc_chunks(&pks_and_chunks, epoch, &associated_data, sys, rng);

// Passes all public verification
assert!(verify_ciphertext_integrity(&crsz, epoch, &associated_data, sys).is_ok());
// verify_chunking also passes (NIZK does not bound individual chunks)

// On load_transcript, every node hits:
// CheatingDealerDlogSolver::new(40, 16) -> ~1 GiB allocation, hours of BSGS
let _ = dec_chunks(secret_key, i, &crsz, epoch, &associated_data);
```

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_chunking.rs (L343-350)
```rust
    let ss = n * m * (CHUNK_SIZE - 1) * CHALLENGE_MASK;
    let zz = 2 * NUM_ZK_REPETITIONS * ss;
    let zz_big = Scalar::from_usize(zz);

    for z_sk in nizk.z_s.iter() {
        if z_sk >= &zz_big {
            return Err(ZkProofChunkingError::InvalidProof);
        }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/tests/forward_secure.rs (L193-194)
```rust
    // still a valid ciphertext
    assert!(verify_ciphertext_integrity(&crsz, epoch, &associated_data, sys).is_ok());
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs (L882-893)
```rust
    for i in 0..NUM_CHUNKS {
        let r = &crsz.rr[i];
        let s = &crsz.ss[i];
        let z = G2Prepared::from(&crsz.zz[i]);

        let v = Gt::multipairing(&[(r, &precomp_id), (s, &sys.h_prep), (&g1_neg, &z)]);

        if !v.is_identity() {
            return Err(InvalidNidkgCiphertext);
        }
    }
    Ok(())
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L340-340)
```rust
    const MAX_TABLE_MBYTES: usize = 2 * 1024; // 2 GiB
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/encryption.rs (L247-250)
```rust
    let ciphertext = crypto::FsEncryptionCiphertext::deserialize(ciphertext)
        .map_err(DecryptError::MalformedCiphertext)?;
    crypto::dec_chunks(secret_key, index, &ciphertext, epoch, associated_data)
        .map_err(|e| DecryptError::InvalidChunk(format!("{e:?}")))
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
