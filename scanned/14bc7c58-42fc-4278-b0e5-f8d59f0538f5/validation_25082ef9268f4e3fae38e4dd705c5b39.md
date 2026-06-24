### Title
DKG Pool Hit Bypass Allows Stale Remote Dealings Into Finalized Blocks — (`rs/consensus/dkg/src/payload_validator.rs`)

---

### Summary

The `validated_contains` fast-path in `validate_dealings_payload` unconditionally skips both the config-existence check and `crypto_validate_dealing` for any dealing already present in the validator's local DKG pool. A single Byzantine block proposer (below fault threshold) can exploit this to include dealings for cancelled/timed-out remote DKG configs in finalized blocks, violating the invariant that every dealing in a finalized block must correspond to an active config, and starving legitimate dealings from `max_dealings_per_payload` slots.

---

### Finding Description

In `validate_dealings_payload`, the per-message loop is:

```rust
for message in &dealings.messages {
    // Skip the rest if already present in DKG pool
    if dkg_pool.validated_contains(message) {
        continue;                          // ← skips config check AND crypto
    }
    let Some(config) = configs.get(&message.content.dkg_id) else {
        return Err(MissingDkgConfigForDealing.into());
    };
    crypto_validate_dealing(crypto, config, message)?;
}
``` [1](#0-0) 

The `configs` map is freshly built from the current certified replicated state on every call: [2](#0-1) 

The DKG pool is only purged at DKG interval boundaries (`ChangeAction::Purge(height)` removes entries with `id.height < height`): [3](#0-2) 

`validate_dealings_for_dealer` in `lib.rs` deliberately **defers** (does not reject) remote DKG dealings when no config is found yet, so they accumulate in the unvalidated pool and are moved to validated once the config appears: [4](#0-3) 

The attack sequence:

1. Remote DKG context exists → honest node's dealing is validated into the pool (`MoveToValidated`).
2. Remote DKG context is cancelled/timed-out → removed from replicated state mid-interval.
3. `configs` no longer contains the dealing's `dkg_id` (it is absent from both `last_summary.configs` and `remote_config_results`).
4. Byzantine proposer crafts a `DkgDataPayload` containing that dealing.
5. Every honest validator calls `dkg_pool.validated_contains(message)` → `true` → `continue` → the config-existence check at line 218 is never reached → `Ok(())`. [5](#0-4) 

---

### Impact Explanation

- **Invariant violation**: Finalized blocks contain dealings for DKG configs that no longer exist in replicated state.
- **Slot starvation / liveness degradation**: `max_dealings_per_payload` is a hard cap checked before the per-message loop. A Byzantine proposer fills all slots with stale dealings, preventing legitimate dealings from being included in that block. Repeated across multiple rounds this delays or prevents DKG transcript completion.
- **Not a safety break**: The DKG summary creator ignores dealings for absent configs, so no incorrect transcript is produced. The impact is confined to liveness. [6](#0-5) 

---

### Likelihood Explanation

- Requires only **one** Byzantine replica acting as block proposer (normal rotation, no threshold corruption needed).
- Remote DKG context cancellation is a normal protocol event (subnet creation timeout, reshare cancellation).
- The stale dealing is already gossiped to all peers and present in their validated pools — no forgery required.
- The window is the remainder of the current DKG interval after context removal.

---

### Recommendation

Move the `validated_contains` fast-path **after** the config-existence check, or add an explicit config-existence guard before the `continue`:

```rust
for message in &dealings.messages {
    let Some(config) = configs.get(&message.content.dkg_id) else {
        return Err(InvalidDkgPayloadReason::MissingDkgConfigForDealing.into());
    };
    if dkg_pool.validated_contains(message) {
        continue;   // config still exists; safe to skip crypto re-validation
    }
    crypto_validate_dealing(crypto, config, message)?;
}
```

This preserves the performance optimisation (skip expensive crypto for already-validated dealings) while enforcing that the config must still be active.

---

### Proof of Concept

State-machine test outline:

1. Set up a subnet with a live remote DKG context; let `DkgImpl::on_state_change` produce and validate a dealing into the pool (`AddToValidated` / `MoveToValidated`).
2. Remove the remote DKG context from the mock `StateManager` (simulate cancellation).
3. Construct a `DkgDataPayload` containing that dealing.
4. Call `validate_payload` with the updated state (context absent) and the populated DKG pool.
5. Assert the result is `Ok(())` — demonstrating the bypass — and assert `MissingDkgConfigForDealing` is **not** returned despite the config being absent. [7](#0-6) 

The existing test `test_validate_payload_dealings_registry_version` already demonstrates that `validate_payload` returns `Ok(())` when the dealing is in the validated pool — the missing step is simply removing the config from state first, which the test does not do.

### Citations

**File:** rs/consensus/dkg/src/payload_validator.rs (L164-170)
```rust
    if dealings.messages.len() > max_dealings_per_payload {
        return Err(InvalidDkgPayloadReason::TooManyDealings {
            limit: max_dealings_per_payload,
            actual: dealings.messages.len(),
        }
        .into());
    }
```

**File:** rs/consensus/dkg/src/payload_validator.rs (L193-205)
```rust
    let state = state_reader
        .get_state_at(validation_context.certified_height)
        .map_err(DkgPayloadCreationError::StateManagerError)?;

    let remote_config_results = build_callback_id_config_map(
        subnet_id,
        registry_client,
        state.get_ref(),
        validation_context.registry_version,
        last_summary,
        log,
    )?;
    let configs = merge_configs(&last_summary.configs, &remote_config_results);
```

**File:** rs/consensus/dkg/src/payload_validator.rs (L209-224)
```rust
    for message in &dealings.messages {
        metrics.with_label_values(&["total"]).inc();

        // Skip the rest if already present in DKG pool
        if dkg_pool.validated_contains(message) {
            metrics.with_label_values(&["dkg_pool_hit"]).inc();
            continue;
        }

        let Some(config) = configs.get(&message.content.dkg_id) else {
            return Err(InvalidDkgPayloadReason::MissingDkgConfigForDealing.into());
        };

        // Verify the signature and dealing.
        crypto_validate_dealing(crypto, config, message)?;
    }
```

**File:** rs/consensus/dkg/src/payload_validator.rs (L856-874)
```rust
            // Add the dealing to the validated pool
            dkg_pool.apply(vec![ChangeAction::AddToValidated(dealing.clone())]);

            // It should be possible to validate the dealing as part of a block payload,
            // even if the dealing is already part of the validated pool
            let result = validate_payload(
                subnet_id,
                registry.as_ref(),
                crypto.as_ref(),
                &PoolReader::new(&pool),
                &dkg_pool,
                parent,
                &block_payload,
                state_manager.as_ref(),
                &context,
                &mock_metrics(),
                &no_op_logger(),
            );
            assert!(result.is_ok());
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L59-82)
```rust
    fn purge(&mut self, height: Height) -> Vec<DkgMessageId> {
        self.current_start_height = height;
        // TODO: use drain_filter once it's stable.
        let unvalidated_keys: Vec<_> = self
            .unvalidated
            .keys()
            .filter(|id| id.height < height)
            .cloned()
            .collect();
        for id in unvalidated_keys {
            self.unvalidated.remove(&id);
        }

        let validated_keys: Vec<_> = self
            .validated
            .keys()
            .filter(|id| id.height < height)
            .cloned()
            .collect();
        for hash in &validated_keys {
            self.validated.remove(hash);
        }
        validated_keys
    }
```

**File:** rs/consensus/dkg/src/lib.rs (L207-219)
```rust
        let config = match configs.get(message_dkg_id) {
            Some(config) => config,
            None if message_dkg_id.target_subnet.is_remote() => {
                return Mutations::new();
            }
            None => {
                return get_handle_invalid_change_action(
                    message,
                    format!("No DKG configuration for Id={message_dkg_id:?} was found."),
                )
                .into();
            }
        };
```
