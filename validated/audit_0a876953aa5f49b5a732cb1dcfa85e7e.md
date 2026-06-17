### Title
`new_settlement_layer_chain_id_storage` Not Rolled Back on Frame Revert — Stale State Persists Across Transaction Boundaries - (`basic_system/src/system_implementation/system/io_subsystem.rs`)

---

### Summary

`FullIO::begin_next_tx()` resets `storage`, `transient_storage`, `logs_storage`, and `events_storage` between transactions, but never resets `new_settlement_layer_chain_id_storage`. Worse, `FullIOStateSnapshot` does not include a snapshot handle for `new_settlement_layer_chain_id_storage`, so `finish_io_frame()` never rolls it back when a call frame or transaction reverts. A reverted transaction that emits the `SettlementLayerChainIdUpdated` event signature permanently poisons `new_settlement_layer_chain_id_storage` for the entire block, causing the block-finalization assertion in `read_batch_context_inputs` to panic and making the block unprovable.

---

### Finding Description

`FullIO` holds six per-block subsystems. Five of them participate in the transaction-boundary reset and the frame-level snapshot/rollback protocol; `new_settlement_layer_chain_id_storage` does not.

**`begin_next_tx` omits `new_settlement_layer_chain_id_storage`:** [1](#0-0) 

**`FullIOStateSnapshot` has no field for it:** [2](#0-1) 

**`start_io_frame` / `finish_io_frame` never touch it:** [3](#0-2) 

By contrast, `interop_root_storage` is correctly snapshotted and rolled back in the same functions.

The update path is the `system_context_event_hook`, which fires for **any** contract emitting the `SettlementLayerChainIdUpdated` topic — it does not restrict the caller to `SystemContext`: [4](#0-3) 

The hook calls `update_settlement_layer_chain_id`, which writes into `new_settlement_layer_chain_id_storage`: [5](#0-4) 

`NewSettlementLayerChainIdStorage::update` enforces a once-per-block guard: [6](#0-5) 

At block finalization, `read_batch_context_inputs` asserts that the stored value matches the on-chain storage value: [7](#0-6) 

**Attack sequence:**

1. Attacker deploys a contract that emits `SettlementLayerChainIdUpdated(attacker_value)` and then explicitly reverts.
2. Attacker submits a transaction calling that contract.
3. During execution the event hook fires → `new_settlement_layer_chain_id_storage` is set to `attacker_value`.
4. The transaction reverts. `finish_io_frame(Some(&rollback))` rolls back `events_storage` (the event disappears) but **does not touch** `new_settlement_layer_chain_id_storage` — it retains `attacker_value`.
5. Block finalization reads the actual SL chain ID from `SystemContext` storage (unchanged, because the tx reverted) and compares it against `attacker_value`. The `assert_eq!` panics → the block cannot be finalized or proved.

Additionally, the once-per-block guard now permanently blocks any legitimate `SettlementLayerChainIdUpdated` transaction later in the same block.

---

### Impact Explanation

- **Block unprovability / DoS**: The `assert_eq!` in `read_batch_context_inputs` will panic whenever `new_settlement_layer_chain_id_storage` holds a value that does not match the actual on-chain SL chain ID. Because the storage was never updated (the tx reverted), the mismatch is guaranteed. This makes the block impossible to finalize or prove.
- **Permanent lock-out of legitimate SL chain ID updates**: The once-per-block guard in `NewSettlementLayerChainIdStorage::update` treats the stale value as a committed update, silently rejecting any real update for the rest of the block.

---

### Likelihood Explanation

Any unprivileged transaction sender can trigger this. No special role, key, or governance access is required. The attacker only needs to deploy a contract that emits the correct 32-byte event topic and reverts. The event hook does not validate the emitting address. The attack is deterministic and reproducible on every block.

---

### Recommendation

1. **Include `new_settlement_layer_chain_id_storage` in `FullIOStateSnapshot`** and roll it back in `finish_io_frame`, mirroring the treatment of `interop_root_storage`.
2. **Reset `new_settlement_layer_chain_id_storage` in `begin_next_tx`** so stale state from a previous transaction cannot bleed into the next one.
3. **Restrict the event hook** to only fire when the emitting contract is the canonical `SystemContext` address, preventing arbitrary contracts from triggering the update path.

---

### Proof of Concept

```
Block with two transactions:

Tx 1 (attacker):
  - Calls attacker contract
  - Contract emits SettlementLayerChainIdUpdated(topic=SL_CHAIN_ID_UPDATED_EVENT_SIG, data=attacker_id)
  - system_context_event_hook fires → new_settlement_layer_chain_id_storage = attacker_id
  - Contract reverts (REVERT opcode)
  - finish_io_frame rolls back events_storage (event removed) but NOT new_settlement_layer_chain_id_storage
  - new_settlement_layer_chain_id_storage still holds attacker_id

Block finalization:
  - read_batch_context_inputs reads settlement_layer_chain_id from SystemContext storage = original_id
  - new_settlement_layer_chain_id_storage.value() = Some(attacker_id)
  - assert_eq!(attacker_id, original_id) → PANIC → block unprovable
```

### Citations

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L61-67)
```rust
pub struct FullIOStateSnapshot<M: StorageModel> {
    io: M::StateSnapshot,
    transient: CacheSnapshotId,
    messages: usize,
    events: usize,
    interop_roots: usize,
}
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L248-264)
```rust
    fn update_settlement_layer_chain_id(
        &mut self,
        _ee_type: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        new_sl_chain_id: U256,
    ) -> Result<(), SystemError> {
        // For native we charge just for the storage
        let native = <Self::Resources as Resources>::Native::from_computational(
            NEW_SL_CHAIN_ID_STORAGE_NATIVE_COST,
        );

        let to_charge = Self::Resources::from_native(native);
        resources.charge(&to_charge)?;

        self.new_settlement_layer_chain_id_storage
            .update(new_sl_chain_id)
    }
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L402-433)
```rust
    fn start_io_frame(&mut self) -> Result<Self::StateSnapshot, InternalError> {
        let io = self.storage.start_frame();
        let transient = self.transient_storage.start_frame();
        let messages = self.logs_storage.start_frame();
        let events = self.events_storage.start_frame();
        let interop_roots = self.interop_root_storage.start_frame();

        Ok(FullIOStateSnapshot {
            io,
            transient,
            messages,
            events,
            interop_roots,
        })
    }

    fn finish_io_frame(
        &mut self,
        rollback_handle: Option<&Self::StateSnapshot>,
    ) -> Result<(), InternalError> {
        self.storage.finish_frame(rollback_handle.map(|x| &x.io))?;
        self.transient_storage
            .finish_frame(rollback_handle.map(|x| &x.transient))?;
        self.logs_storage
            .finish_frame(rollback_handle.map(|x| x.messages));
        self.events_storage
            .finish_frame(rollback_handle.map(|x| x.events));
        self.interop_root_storage
            .finish_frame(rollback_handle.map(|x| x.interop_roots));

        Ok(())
    }
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L523-528)
```rust
    fn begin_next_tx(&mut self) {
        self.storage.begin_new_tx();
        self.transient_storage.begin_new_tx();
        self.logs_storage.begin_new_tx();
        self.events_storage.begin_new_tx();
    }
```

**File:** system_hooks/src/event_hooks/system_context.rs (L18-36)
```rust
pub fn system_context_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    if topics.is_empty() {
        return Ok(());
    }
    // For now, we only capture the SettlementLayerChainIdUpdated event
    if topics[0].as_u8_array() == SL_CHAIN_ID_UPDATED_EVENT_SIG {
        new_sl_chain_id_event_hook(topics, data, caller_ee, system, resources)
    } else {
        Ok(())
    }
}
```

**File:** zk_ee/src/common_structs/new_settlement_layer_chain_id_storage.rs (L41-51)
```rust
    pub fn update(&mut self, new_sl_chain_id: U256) -> Result<(), SystemError> {
        if self.value().is_some() {
            return Err(internal_error!(
                "Tried to update settlement layer chain id more than once in a block"
            )
            .into());
        }
        self.history.update(new_sl_chain_id);

        Ok(())
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L232-240)
```rust
    let multichain_root = read_multichain_root(io);
    let settlement_layer_chain_id = read_settlement_layer_chain_id(io);
    if let Some(new_settlement_layer_chain_id) = io.new_settlement_layer_chain_id_storage.value() {
        // If the SL chain id was updated, make sure the updated one matches
        // the one read from storage.
        assert_eq!(new_settlement_layer_chain_id, &settlement_layer_chain_id);
    }

    (multichain_root, settlement_layer_chain_id)
```
