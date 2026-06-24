Based on my thorough code analysis, here is my assessment:

---

### Title
Byzantine Dealer Can Trigger Unbounded BSGS Computation in `dec_chunks`, Blocking DKG Key Loading for Hours — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/forward_secure.rs`)

### Summary

A single Byzantine dealer below the fault threshold can craft a dealing whose ciphertext passes all ZK proof verifications (`verify_ciphertext_integrity`, `verify_chunking`, `verify_sharing`) but encodes out-of-range plaintext chunks. When any receiver node calls `load_threshold_signing_key`, the `dec_chunks` function falls through to `CheatingDealerDlogSolver::solve()`, which runs Baby-Step Giant-Step (BSGS) that the code itself acknowledges **"may take hours."** There is no timeout on this computation.

### Finding Description

The call chain is:

`load_threshold_signing_key` → `load_threshold_signing_key_internal` → `ni_dkg_clib::compute_threshold_signing_key` → `decrypt` → `dec_chunks`

In `dec_chunks`, after the fast `HonestDealerDlogLookupTable` fails to find the discrete log (because the chunk is outside `[CHUNK_MIN, CHUNK_MAX]`), the code falls through to: [1](#0-0) 

The comment at line 830 explicitly states: **"It may take hours to brute force a cheater's discrete log."**

The `CheatingDealerDlogSolver` is designed for exactly this scenario — a dealer whose NIZK chunking proof verifies but whose chunks are outside the honest range: [2](#0-1) 

The BSGS outer loop iterates over `scale_range = 1 << CHALLENGE_BITS = 256` delta values, and for each runs a full BSGS search over a range of size `2*zz - 1` where `zz = 2 * NUM_ZK_REPETITIONS * n * m * (CHUNK_SIZE - 1) * (scale_range - 1)` — an astronomically large range for production subnet sizes: [3](#0-2) 

The ZK proof system does **not** prevent this attack. The chunking proof bounds `z_s` values but does not tightly bound the actual plaintext chunks to `[0, 65535]`. The `CheatingDealerDlogSolver` exists precisely because a cheating dealer can pass `verify_chunking` with out-of-range chunks. This is confirmed by the production test: [4](#0-3) 

The `load_threshold_signing_key_internal` function does **not** hold the SKS write lock during `compute_threshold_signing_key` — the write lock is only acquired after the computation completes: [5](#0-4) 

However, the computation runs on the tarpc server's rayon thread pool: [6](#0-5) 

A blocked thread pool thread for hours means the thread pool can be exhausted, blocking all subsequent crypto vault operations on that node.

### Impact Explanation

- Every receiver node that calls `load_threshold_signing_key` for a transcript containing the Byzantine dealer's dealing will block a thread pool thread for hours per out-of-range chunk (up to 16 chunks per dealing).
- With multiple Byzantine dealers (up to `f = floor((n-1)/3)` in a subnet), the effect is multiplied.
- Thread pool exhaustion blocks all crypto operations on the affected node.
- DKG round completion is delayed, potentially preventing the subnet from rotating threshold keys on schedule.

### Likelihood Explanation

- Requires being a registered subnet node acting as a dealer — a protocol peer below the fault threshold, which is an explicitly in-scope attacker entry point.
- The attack is straightforward: craft chunks outside `[0, 65535]` but within the ZK proof's slack range, generate a valid ZK proof, submit the dealing.
- The code explicitly acknowledges this scenario and provides `CheatingDealerDlogSolver` as the designed (but slow) response.
- The `#[ignore]`d test `print_time_for_cheating_dlog_solver_to_run` exists specifically to measure this cost. [7](#0-6) 

### Recommendation

1. **Add a timeout** to the BSGS computation in `dec_chunks`. If the computation exceeds a bound (e.g., 60 seconds), return `Err(DecErr::InvalidChunk)` and reject the dealing's contribution rather than blocking indefinitely.
2. **Reject the dealing at transcript creation time** if any chunk requires BSGS — i.e., add a fast pre-check that all decrypted chunks fall in `[CHUNK_MIN, CHUNK_MAX]` before including a dealing in the transcript.
3. **Tighten the ZK chunking proof** to more tightly bound the plaintext chunks, reducing or eliminating the slack that allows out-of-range chunks to pass verification.
4. **Run BSGS in a separate bounded thread** with a timeout, not on the main crypto thread pool.

### Proof of Concept

1. As a registered subnet dealer, create a dealing where one or more plaintext chunks for a target receiver are set to a value outside `[0, 65535]` but within the ZK proof's slack range (e.g., `chunk * delta` for small `delta`).
2. Generate a valid chunking and sharing ZK proof (the proof system allows this slack by design).
3. Submit the dealing; it passes `verify_dealing` on all honest nodes.
4. The dealing is included in the DKG transcript.
5. When any receiver calls `load_transcript` → `load_threshold_signing_key_internal`, measure wall-clock time of `dec_chunks` — it will run `CheatingDealerDlogSolver::solve()` for each out-of-range chunk, taking hours per chunk. [8](#0-7)

### Citations

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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/dlog_recovery.rs (L368-374)
```rust
    /// Searches for discrete log for a malicious DKG participant whose NIZK
    /// chunking proof checks out, which implies certain bounds on the search
    ///
    /// This function is not constant time, but it only leaks information in the
    /// event that a dealer is already dishonest, and the only information it
    /// leaks is the value that the dishonest dealer sent.
    pub fn solve(&self, target: &Gt) -> Option<Scalar> {
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

**File:** rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/ni_dkg/mod.rs (L494-506)
```rust
                let csp_secret_key = ni_dkg_clib::compute_threshold_signing_key(
                    &transcript,
                    receiver_index,
                    &fs_decryption_key,
                    epoch,
                )
                .map(CspSecretKey::ThresBls12_381)?;

                let result = self.sks_write_lock().insert(
                    threshold_key_id,
                    csp_secret_key,
                    Some(NIDKG_THRESHOLD_SCOPE),
                );
```

**File:** rs/crypto/internal/crypto_service_provider/src/vault/remote_csp_vault/tarpc_csp_vault_server.rs (L244-255)
```rust
        let vault = self.local_csp_vault;
        let job = move || {
            vault.load_threshold_signing_key(
                algorithm_id,
                epoch,
                csp_transcript,
                fs_key_id,
                receiver_index,
            )
        };
        execute_on_thread_pool(&self.thread_pool, job).await
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
