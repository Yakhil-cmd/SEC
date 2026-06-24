### Title
Single Node MEGa Key Rotation Triggers Full Subnet Chain-Key Resharing, Invalidating Pre-Signature Stash and Disrupting Threshold Signing - (File: `rs/consensus/idkg/src/payload_builder.rs`)

---

### Summary

Any node on a chain-key signing subnet can rotate its iDKG MEGa encryption key via the permissionless `update_node_directly` registry endpoint. When the rotated key is picked up in the next DKG summary interval, `is_time_to_reshare_key_transcript` detects the key change and forces a full subnet-wide iDKG key resharing. During the resharing period (which spans at least one full DKG interval), the entire pre-signature stash is invalidated and no new pre-signatures can be matched to pending `sign_with_ecdsa`/`sign_with_schnorr` requests. If a `signature_request_timeout_ns` is configured, all queued signing requests expire and are rejected. This is the IC analog of the ZetaChain finding: a single participant's routine action (key rotation) forces a protocol-wide key regeneration that temporarily halts the chain-key signing service.

---

### Finding Description

**Step 1 – Permissionless key rotation entry point**

Any node on a signing subnet can call `update_node_directly` on the registry canister to rotate its iDKG MEGa encryption key. The endpoint is explicitly callable by anyone with a valid node identity: [1](#0-0) 

The underlying `do_update_node` function enforces only timing-based rate limits (the node's own key must be old enough, and the subnet must not have had a recent update), but no governance approval is required: [2](#0-1) 

**Step 2 – Key change detected at DKG summary boundary**

At every DKG summary block, `create_summary_payload_helper` calls `is_time_to_reshare_key_transcript`. This function compares the MEGa public keys of all subnet nodes between the current and next registry versions. If **any single node's key has changed**, it returns `true`: [3](#0-2) 

When this returns `true`, the summary payload sets `next_in_creation = KeyTranscriptCreation::Begin`, triggering a full iDKG key resharing for the subnet: [4](#0-3) 

**Step 3 – Pre-signature stash is invalidated**

Once the resharing completes and a new key transcript is created, the summary payload purges all pre-signatures in creation and all ongoing xnet reshares associated with the old key: [5](#0-4) 

At the execution layer, when a new key transcript is delivered, the entire pre-signature stash is also purged because the stash's `key_transcript.transcript_id` no longer matches: [6](#0-5) 

**Step 4 – Signing requests are rejected during the resharing window**

During the resharing period (at minimum one full DKG interval, typically hundreds of blocks), no pre-signatures are available. Any pending `sign_with_ecdsa` or `sign_with_schnorr` request that has a `signature_request_timeout_ns` configured will expire and be rejected with `"Chain key request expired"`: [7](#0-6) 

The resharing itself takes at least one DKG interval to complete, and the pre-signature stash must then be rebuilt from scratch before signing can resume: [8](#0-7) 

---

### Impact Explanation

- **Chain-key signing is temporarily unavailable** for the entire duration of the resharing process (one or more DKG intervals) plus the time to rebuild the pre-signature stash.
- **All queued signing requests expire** if `signature_request_timeout_ns` is set, causing canisters that depend on threshold ECDSA/Schnorr to receive errors.
- **The pre-signature stash is wiped**, meaning even requests that were already matched to a pre-signature but not yet completed may be disrupted.
- This affects all users of the signing subnet — any canister calling `sign_with_ecdsa`, `sign_with_schnorr`, or cross-subnet key resharing.

---

### Likelihood Explanation

- **Routine operation**: Key rotation is a designed, expected, and automated operation. The orchestrator calls `check_all_keys_registered_otherwise_register` every 10 seconds and will rotate keys when the rotation period expires. [9](#0-8) 
- **No governance required**: The `update_node_directly` endpoint requires no NNS proposal. Any node operator whose node's key has aged past `idkg_key_rotation_period_ms` can trigger this.
- **Predictable timing**: The rotation schedule is deterministic and publicly observable from the registry, so an adversarial node operator can time the rotation to maximize disruption.
- **Amplified by subnet size**: On a large signing subnet, rotations happen frequently (one per node per rotation period), meaning the resharing is triggered repeatedly.

---

### Recommendation

1. **Decouple MEGa key rotation from iDKG key resharing**: Instead of triggering a full resharing on every individual node key change, batch key changes and only reshare at a scheduled interval or when a threshold of nodes have rotated.
2. **Preserve pre-signatures across resharing**: Investigate whether pre-signatures created under the old key transcript can remain valid for already-matched signing requests, rather than being immediately purged.
3. **Rate-limit resharing triggers**: Add a minimum interval between consecutive resharing operations triggered by key rotation, separate from the per-node rotation rate limit.
4. **Increase `signature_request_timeout_ns`**: Operators should set this value to be at least several DKG intervals to tolerate routine resharing without dropping requests.

---

### Proof of Concept

1. Deploy a subnet with chain-key signing enabled and `idkg_key_rotation_period_ms` set (e.g., 2 weeks).
2. Queue several `sign_with_ecdsa` requests with a short `signature_request_timeout_ns`.
3. Wait for (or manually trigger via `update_node_directly`) a single node's MEGa key rotation to be accepted by the registry.
4. Observe that at the next DKG summary block, `is_time_to_reshare_key_transcript` returns `true` due to the key change: [10](#0-9) 
5. Observe that `next_in_creation` is set to `Begin`, the pre-signature stash is purged, and all queued signing requests are rejected with `"Chain key request expired"`.
6. The system test `tecdsa_key_rotation_test` already confirms this behavior — the stash drops to 0 after each rotation: [11](#0-10)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L1227-1235)
```rust
#[unsafe(export_name = "canister_update update_node_directly")]
fn update_node_directly() {
    // This method can be called by anyone
    println!(
        "{}call: update_node_directly from: {}",
        LOG_PREFIX,
        dfn_core::api::caller()
    );
    over(candid_one, update_node_directly_);
```

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L33-42)
```rust
    pub fn do_update_node_directly(
        &mut self,
        payload: UpdateNodeDirectlyPayload,
    ) -> Result<(), String> {
        println!("{LOG_PREFIX}do_update_node_directly: {payload:?}");
        // We pull out the caller retrieval and determining of the current time, so that we can unit test the underlying function
        // with any node id.
        let node_id = NodeId::from(dfn_core::api::caller());
        self.do_update_node(now(), node_id, payload)
    }
```

**File:** rs/consensus/idkg/src/payload_builder.rs (L302-327)
```rust
        // Check for membership change, start next key creation only when both of the following are
        // satisfied:
        // 1. Time to reshare key transcript (either due to membership change, or node key change)
        // 2. We don't have a key transcript creation in progress.
        let next_in_creation = if is_time_to_reshare_key_transcript(
            registry_client,
            curr_key_registry_version,
            next_interval_registry_version,
            subnet_id,
        )? && created_key_transcript.is_some()
        {
            info!(
                log,
                "Noticed subnet membership or mega encryption key change for key id {}, \
                will start key_transcript_creation: height = {} \
                current_version = {}, next_version = {}",
                key_id,
                height,
                curr_key_registry_version,
                next_interval_registry_version
            );
            idkg::KeyTranscriptCreation::Begin
        } else {
            // No change, just carry forward the next_in_creation transcript
            key_transcript.next_in_creation.clone()
        };
```

**File:** rs/consensus/idkg/src/payload_builder.rs (L357-365)
```rust
    // We purge the pre-signatures in creation for changed key transcripts.
    idkg_summary
        .pre_signatures_in_creation
        .retain(|_, pre_sig| !new_key_transcripts.contains(&pre_sig.key_id()));
    // This will clear the current ongoing reshares, and the execution requests will be restarted
    // with the new key and different transcript IDs.
    idkg_summary
        .ongoing_xnet_reshares
        .retain(|request, _| !new_key_transcripts.contains(&request.master_key_id));
```

**File:** rs/consensus/idkg/src/payload_builder.rs (L427-459)
```rust
fn is_time_to_reshare_key_transcript(
    registry_client: &dyn RegistryClient,
    curr_registry_version: RegistryVersion,
    next_registry_version: RegistryVersion,
    subnet_id: SubnetId,
) -> Result<bool, MembershipError> {
    // Shortcut the case where registry version didn't change
    if curr_registry_version == next_registry_version {
        return Ok(false);
    }
    let current_nodes = get_subnet_nodes_(registry_client, curr_registry_version, subnet_id)?
        .into_iter()
        .collect::<BTreeSet<_>>();
    let next_nodes = get_subnet_nodes(registry_client, next_registry_version, subnet_id)?
        .into_iter()
        .collect::<BTreeSet<_>>();
    if current_nodes != next_nodes {
        return Ok(true);
    }
    // Check if node's key has changed, which should also trigger key transcript resharing.
    for node in current_nodes {
        let curr_key =
            retrieve_mega_public_key_from_registry(&node, registry_client, curr_registry_version)
                .map_err(MembershipError::MegaKeyFromRegistryError)?;
        let next_key =
            retrieve_mega_public_key_from_registry(&node, registry_client, next_registry_version)
                .map_err(MembershipError::MegaKeyFromRegistryError)?;
        if curr_key != next_key {
            return Ok(true);
        }
    }
    Ok(false)
}
```

**File:** rs/execution_environment/src/scheduler/threshold_signatures.rs (L54-59)
```rust
    // Purge all pre-signature stashes for which a different (or no) key transcript was delivered.
    pre_signature_stashes.retain(|key_id, stash| {
        delivered_pre_signatures.get(key_id).is_some_and(|data| {
            data.key_transcript.transcript_id == stash.key_transcript.transcript_id
        })
    });
```

**File:** rs/consensus/chain_key/src/lib.rs (L596-604)
```rust
                                ChainKeyErrorCode::TimedOut => RejectContext::new(
                                    RejectCode::SysTransient,
                                    "Chain key request expired",
                                ),
                                ChainKeyErrorCode::InvalidKey => RejectContext::new(
                                    RejectCode::SysTransient,
                                    "Invalid or disabled key_id in request",
                                ),
                            })
```

**File:** rs/consensus/idkg/src/payload_builder/key_transcript.rs (L60-112)
```rust
pub(super) fn update_next_key_transcript(
    key_transcript: &mut MasterKeyTranscript,
    uid_generator: &mut IDkgUIDGenerator,
    receivers: &[NodeId],
    registry_version: RegistryVersion,
    transcript_cache: &dyn IDkgTranscriptBuilder,
    height: Height,
    log: &ReplicaLogger,
) -> Result<Option<IDkgTranscript>, IDkgPayloadError> {
    let mut new_transcript = None;
    match (&key_transcript.current, &key_transcript.next_in_creation) {
        (Some(transcript), idkg::KeyTranscriptCreation::Begin) => {
            // We have an existing key transcript, need to reshare it to create next
            // Create a new reshare config when there is none
            let dealers = transcript.receivers();
            let receivers_set = receivers.iter().copied().collect::<BTreeSet<_>>();
            info!(
                log,
                "Reshare IDkg key transcript from dealers {:?} to receivers {:?}, height = {}",
                dealers,
                receivers,
                height,
            );
            key_transcript.next_in_creation = idkg::KeyTranscriptCreation::ReshareOfUnmaskedParams(
                idkg::ReshareOfUnmaskedParams::new(
                    uid_generator.next_transcript_id(),
                    receivers_set,
                    registry_version,
                    transcript,
                    transcript.unmasked_transcript(),
                ),
            );
        }

        (Some(_), idkg::KeyTranscriptCreation::ReshareOfUnmaskedParams(config)) => {
            // check if the next key transcript has been made
            if let Some(transcript) =
                transcript_cache.get_completed_transcript(config.as_ref().transcript_id)
            {
                info!(
                    log,
                    "IDkg key transcript created from ReshareOfUnmasked {:?} \
                    registry_version {} height = {}",
                    config.as_ref().transcript_id,
                    transcript.registry_version,
                    height,
                );
                let transcript_ref = idkg::UnmaskedTranscript::try_from((height, &transcript))?;
                key_transcript.next_in_creation =
                    idkg::KeyTranscriptCreation::Created(transcript_ref);
                new_transcript = Some(transcript);
            }
        }
```

**File:** rs/orchestrator/src/registration.rs (L380-435)
```rust
    pub async fn check_all_keys_registered_otherwise_register(&self, subnet_id: SubnetId) {
        let registry_version = self.registry_client.get_latest_version();
        // If there is no Chain key config or no key_ids, threshold signing is disabled.
        // Delta is the key rotation period of a single node, if it is None, key rotation is disabled.
        let Some(delta) = self.get_key_rotation_period(registry_version, subnet_id) else {
            self.metrics
                .observe_key_rotation_status(KeyRotationStatus::Disabled);
            return;
        };

        let key_handler = self.key_handler.clone();
        if let Err(e) = tokio::task::spawn_blocking(move || {
            key_handler.check_keys_with_registry(registry_version)
        })
        .await
        .unwrap()
        {
            self.metrics.observe_key_rotation_error();
            warn!(self.log, "Failed to check keys with registry: {e:?}");
            UtilityCommand::notify_host(
                format!("Failed to check keys with registry: {e:?}").as_str(),
                1,
            );
        }

        if !self.is_time_to_rotate(registry_version, subnet_id, delta) {
            self.metrics
                .observe_key_rotation_status(KeyRotationStatus::TooRecent);
            return;
        }

        // Call crypto to check if the local node should rotate its keys, and potentially
        // try to register the new key, or a previously rotated key that was not yet
        // registered.
        // In case registration of a key fails, we will enter this branch
        // during the next call and retry registration.
        let key_handler = self.key_handler.clone();
        self.metrics
            .observe_key_rotation_status(KeyRotationStatus::Rotating);
        match tokio::task::spawn_blocking(move || {
            key_handler.rotate_idkg_dealing_encryption_keys(registry_version)
        })
        .await
        .unwrap()
        {
            Ok(IDkgKeyRotationResult::IDkgDealingEncPubkeyNeedsRegistration(rotation_outcome)) => {
                self.register_key(PublicKey::from(rotation_outcome)).await
            }
            Ok(IDkgKeyRotationResult::LatestRotationTooRecent) => {}
            Err(e) => {
                self.metrics.observe_key_rotation_error();
                warn!(self.log, "Key rotation error: {e:?}");
                UtilityCommand::notify_host(format!("Key rotation error: {e:?}").as_str(), 1);
            }
        }
    }
```

**File:** rs/tests/consensus/tecdsa/tecdsa_key_rotation_test.rs (L117-118)
```rust
        // Stash size should be 0 after the roation
        await_pre_signature_stash_size_async(&app_subnet, 0, key_ids.as_slice(), &log).await;
```
