### Title
TOCTOU Race Between iDKG Canister Secret Share Retention and Transcript Loading Can Delete Active Threshold Keys - (File: `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/idkg/mod.rs`)

---

### Summary

`idkg_retain_active_canister_secret_shares` in `LocalCspVault` performs a check-then-act pattern across two separate lock acquisitions on the canister secret key store (`canister_sks`). Between releasing the read lock and acquiring the write lock, a concurrent `idkg_load_transcript_internal` call can insert a newly-computed iDKG threshold key share. The subsequent `retain` write then deletes that freshly-inserted key because it was not present in the `active_key_ids` snapshot taken before the race window. The code itself explicitly acknowledges this consequence.

---

### Finding Description

In `idkg_retain_active_canister_secret_shares`:

```
Step 1 – read-lock check (line 637-638):
    self.canister_sks_read_lock()
        .retain_would_modify_keystore(filter.clone(), IDKG_THRESHOLD_KEYS_SCOPE)
    // read lock dropped here

Step 2 – RACE WINDOW – another thread calls idkg_load_transcript_internal (line 366-370):
    self.canister_sks_write_lock().insert_or_replace(
        KeyId::from(transcript.combined_commitment.commitment()),
        CspSecretKey::IDkgCommitmentOpening(opening_bytes),
        Some(IDKG_THRESHOLD_KEYS_SCOPE),
    )

Step 3 – write-lock retain (line 652-656):
    self.canister_sks_write_lock()
        .retain(filter, IDKG_THRESHOLD_KEYS_SCOPE)
    // deletes the key inserted in Step 2 because it is not in active_key_ids
```

The code comment at lines 646–651 explicitly states:

> "Another potential issue is that a new transcript could have been loaded, and a new key added, between the time that retain on the crypto component was called, and the time that we actually call retain here. In this case, a **newly-created key may be deleted**."

The concurrent execution path is real: in `dkg_key_manager.rs`, `load_transcript` is dispatched in a `std::thread::spawn` (line 339) and `delete_inactive_keys` also spawns a separate thread (line 461) that calls `retain_only_active_keys` → `idkg_retain_active_keys` → `idkg_retain_active_canister_secret_shares`. Both threads share the same `LocalCspVault` (via `Arc`) and contend on `canister_sks`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The `canister_sks` (canister secret key store) under `IDKG_THRESHOLD_KEYS_SCOPE` holds the iDKG commitment openings (secret shares) that a node uses to participate in tECDSA and tSchnorr threshold signing. If a freshly-loaded secret share for an active transcript is silently deleted by the racing `retain` call, the affected node:

1. Cannot produce a partial signature for that transcript's signing rounds.
2. Must go through the complaint/opening recovery protocol, which requires cooperation from other nodes and adds latency.
3. If the deletion recurs across multiple transcripts or nodes, the subnet's ability to produce chain-key signatures (ckBTC, ckETH, cross-chain operations) is degraded or halted.

The deletion is silent — no error is returned, no log is emitted — so the node will not detect the loss until a signing attempt fails. [6](#0-5) 

---

### Likelihood Explanation

The race window is narrow but structurally present: both the transcript-load thread and the key-removal thread are spawned concurrently from `DkgKeyManager` and share the same `Arc<LocalCspVault>`. The race requires that a new transcript's secret share computation completes and calls `insert_or_replace` on `canister_sks` precisely between the read-lock release and the write-lock acquisition in `idkg_retain_active_canister_secret_shares`. Key rotation events (which trigger both operations simultaneously) are the highest-risk moments. The code's own comment acknowledges the scenario is possible and defers a fix. The comment's claim that it is "currently not an issue given how the crypto component is called from consensus" is an assertion without a structural guarantee — the concurrent thread spawning provides no ordering constraint between the two operations. [7](#0-6) [8](#0-7) 

---

### Recommendation

Replace the two-phase read-check / write-retain pattern with a single atomic operation that holds the write lock for the entire check-and-retain sequence, eliminating the race window:

```rust
fn idkg_retain_active_canister_secret_shares(
    &self,
    active_key_ids: BTreeSet<KeyId>,
) -> Result<(), IDkgRetainKeysError> {
    let filter = move |key_id: &KeyId, _: &CspSecretKey| active_key_ids.contains(key_id);
    // Acquire write lock once; check and retain atomically.
    let mut write_lock = self.canister_sks_write_lock();
    if write_lock.retain_would_modify_keystore(filter.clone(), IDKG_THRESHOLD_KEYS_SCOPE) {
        write_lock.retain(filter, IDKG_THRESHOLD_KEYS_SCOPE).map_err(|e| ...)?;
    }
    Ok(())
}
```

Alternatively, adopt the registry-version tagging approach mentioned in the code comment so that `retain` can distinguish keys loaded after the retention snapshot was taken. [9](#0-8) 

---

### Proof of Concept

**Thread A** — key removal (spawned by `delete_inactive_keys`):
```
idkg_retain_active_canister_secret_shares(active_key_ids = {K_old})
  → canister_sks_read_lock().retain_would_modify_keystore(...)  → true
  → [read lock released]
  ← PREEMPTED HERE
```

**Thread B** — transcript load (spawned by `load_transcripts_if_necessary`):
```
idkg_load_transcript_internal(transcript = T_new)
  → canister_sks_write_lock().insert_or_replace(K_new, ...)  → Ok
  → [write lock released]
```

**Thread A resumes**:
```
  → canister_sks_write_lock().retain(|k,_| active_key_ids.contains(k), ...)
    // active_key_ids = {K_old}; K_new is NOT in the set → K_new is DELETED
```

Result: `K_new` (the secret share for the newly-loaded active transcript `T_new`) is permanently deleted from the canister secret key store. The node can no longer sign with that transcript's key material. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/idkg/mod.rs (L330-380)
```rust
    fn idkg_load_transcript_internal(
        &self,
        alg: AlgorithmId,
        dealings: &BTreeMap<NodeIndex, IDkgDealingInternal>,
        context_data: &[u8],
        receiver_index: NodeIndex,
        key_id: &KeyId,
        transcript: &IDkgTranscriptInternal,
    ) -> Result<BTreeMap<NodeIndex, IDkgComplaintInternal>, IDkgLoadTranscriptError> {
        if self
            .commitment_opening_from_sks(transcript.combined_commitment.commitment())
            .is_ok()
        {
            // If secret share has already been stored in the C-SKS, nothing to do
            Ok(BTreeMap::new())
        } else {
            let (public_key, private_key) = self.mega_keyset_from_sks(key_id)?;

            let compute_secret_shares_result = compute_secret_shares(
                alg,
                dealings,
                transcript,
                context_data,
                receiver_index,
                &private_key,
                &public_key,
            );

            match compute_secret_shares_result {
                Ok(opening) => {
                    let opening_bytes =
                        CommitmentOpeningBytes::try_from(&opening).map_err(|e| {
                            IDkgLoadTranscriptError::SerializationError {
                                internal_error: format!("{e:?}"),
                            }
                        })?;
                    match self.canister_sks_write_lock().insert_or_replace(
                        KeyId::from(transcript.combined_commitment.commitment()),
                        CspSecretKey::IDkgCommitmentOpening(opening_bytes),
                        Some(IDKG_THRESHOLD_KEYS_SCOPE),
                    ) {
                        Ok(_) => Ok(BTreeMap::new()),
                        Err(SecretKeyStoreWriteError::SerializationError(e)) => {
                            Err(IDkgLoadTranscriptError::InternalError { internal_error: e })
                        }
                        Err(SecretKeyStoreWriteError::TransientError(e)) => {
                            Err(IDkgLoadTranscriptError::TransientInternalError {
                                internal_error: e,
                            })
                        }
                    }
```

**File:** rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/idkg/mod.rs (L580-674)
```rust
    fn idkg_retain_active_keys_internal(
        &self,
        active_canister_key_ids: BTreeSet<KeyId>,
        oldest_public_key: MEGaPublicKey,
    ) -> Result<(), IDkgRetainKeysError> {
        let oldest_public_key_proto = idkg_dealing_encryption_pk_to_proto(oldest_public_key);
        // First check, while only holding a read lock on the PKS, if a call to
        // [`idkg_retain_active_dealing_encryption_public_keys`] would modify the public key store.
        // The reasons for doing this while holding a read lock (and, if necessary, thereafter
        // separately acquiring write locks for both the secret and public key stores) are:
        //  - [`IDkgProtocolCspVault::idkg_retain_active_keys`] is called by consensus around once
        //    per minute, but we expect to actually have to delete old iDKG dealing encryption keys
        //    only at much longer time intervals (on the order of hours/days; configured in the
        //    registry) on production subnets using canister threshold
        //    signatures such as tECDSA or tSchnorr.
        //  - Acquiring both PKS and SKS write locks each time blocks all readers - analysis shows
        //    that they sometimes end up waiting for up to 1 second.
        // The drawback of this approach is that it introduces a race condition - between the time
        // that the PKS read lock is released, and the PKS and SKS write locks are acquired, the
        // key stores could have been modified by another writer. However, this is the lesser of two
        // evils, since:
        //  - We only expect the set of iDKG dealing encryption keys to be modified as part of key
        //    rotation, which doesn't happen that often.
        //  - Even if the key stores are modified by another writer, the worst that can happen is
        //    that we perform unnecessary work, i.e., check the iDKG dealing encryption keys in the
        //    PKS again, and determine that there is no need to delete any key(s). This operation is
        //    quite cheap, since we don't expect there to be more than a handful of keys at any
        //    point in time (most likely just 1-2).
        let would_pks_be_modified = {
            let pks_read_lock = self.public_key_store_read_lock();
            would_idkg_retain_modify_public_key_store(&pks_read_lock, &oldest_public_key_proto)?
        }; // drop read lock on pks

        // If the previous check determined that the public key store would be modified, acquire
        // write locks for both PKS and SKS and try to actually make the modifications.
        if would_pks_be_modified {
            let (sks_write_lock, mut pks_write_lock) = self.sks_and_pks_write_locks();
            let is_pks_modified = idkg_retain_active_dealing_encryption_public_keys(
                &mut pks_write_lock,
                &oldest_public_key_proto,
            )?;
            if is_pks_modified {
                let key_ids_to_keep = idkg_public_key_proto_to_key_id(
                    &pks_write_lock.idkg_dealing_encryption_pubkeys(),
                )?;
                idkg_retain_active_dealing_encryption_secret_keys(sks_write_lock, key_ids_to_keep)?;
            }
        } //drop locks on sks and pks
        self.idkg_retain_active_canister_secret_shares(active_canister_key_ids)
    }

    fn idkg_retain_active_canister_secret_shares(
        &self,
        active_key_ids: BTreeSet<KeyId>,
    ) -> Result<(), IDkgRetainKeysError> {
        let filter = move |key_id: &KeyId, _: &CspSecretKey| active_key_ids.contains(key_id);
        if self
            .canister_sks_read_lock()
            .retain_would_modify_keystore(filter.clone(), IDKG_THRESHOLD_KEYS_SCOPE)
        {
            // The fact that we perform the initial check holding a read lock on the canister SKS,
            // and then possibly acquire a write lock to actually modify the canister SKS, results
            // in a potential race condition here. This has two consequences:
            //  - In case another writer managed to get the write lock after we released the read
            //    lock and acquired the write lock, and also executed the retain operation with the
            //    same set of `active_key_ids`, this is fine, since the operation is idempotent.
            //  - Another potential issue is that a new transcript could have been loaded, and a
            //    new key added, between the time that retain on the crypto component was called,
            //    and the time that we actually call retain here. In this case, a newly-created key
            //    may be deleted. This is currently not an issue given how the crypto component is
            //    called from consensus, but an approach similar to the one proposed for NI-DKG
            //    (adding the registry version to the keys) could be applied here also.
            self.canister_sks_write_lock()
                .retain(
                    filter,
                    IDKG_THRESHOLD_KEYS_SCOPE,
                )
                .map_err(|e| match e {
                    SecretKeyStoreWriteError::SerializationError(e) => {
                        IDkgRetainKeysError::SerializationError {
                            internal_error: format!("Serialization error while retaining active IDKG canister secret shares: {e:?}"),
                        }

                    }
                    SecretKeyStoreWriteError::TransientError(e) => {
                        IDkgRetainKeysError::TransientInternalError {
                            internal_error: format!("IO error while retaining active IDKG canister secret shares: {e:?}")
                        }

                    }
                })
        } else {
            Ok(())
        }
    }
```

**File:** rs/consensus/dkg/src/dkg_key_manager.rs (L329-414)
```rust
        for (deadline, dkg_id) in transcripts_to_load.into_iter() {
            let since = Instant::now();

            let crypto = self.crypto.clone();
            let logger = self.logger.clone();
            let summary = summary.clone();
            let (tx, rx) = sync_channel(0);
            self.pending_transcript_loads
                .insert(dkg_id.clone(), (deadline, rx));

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
        }
    }
```

**File:** rs/consensus/dkg/src/dkg_key_manager.rs (L459-477)
```rust
        let crypto = self.crypto.clone();
        let logger = self.logger.clone();
        let handle = std::thread::spawn(move || {
            match NiDkgAlgorithm::retain_only_active_keys(&*crypto, transcripts_to_retain) {
                Ok(()) => (),
                // If we fail due to a transient error, we simply do nothing.
                // The next delete cycle will remove the keys.
                Err(err) if !err.is_reproducible() => {
                    warn!(
                        logger,
                        "Could not delete DKG keys (Crypto temporarily unavailable): {:?}", err
                    )
                }
                // On a replicated error, we need to log an error
                Err(err) => error!(logger, "Could not delete DKG keys: {:?}", err),
            }
        });
        self.pending_key_removal = Some(handle);
    }
```
