### Title
`new_settlement_layer_chain_id_storage` Excluded from IO Frame Rollback Causes Persistent Stale State on Reverted Transactions — (`basic_system/src/system_implementation/system/io_subsystem.rs`)

---

### Summary

`FullIO` manages six distinct sub-storages. Five of them (`storage`, `transient_storage`, `logs_storage`, `events_storage`, `interop_root_storage`) are correctly snapshotted and rolled back through `start_io_frame` / `finish_io_frame`. The sixth, `new_settlement_layer_chain_id_storage`, is **never snapshotted and never rolled back**, even though it exposes `start_frame()` / `finish_frame()` methods designed for exactly this purpose. Any transaction that triggers a settlement-layer chain-ID update and is subsequently reverted (block-limit revert, out-of-gas, or explicit revert) leaves a stale value in `new_settlement_layer_chain_id_storage` that persists for the rest of the block, breaking block finalization.

---

### Finding Description

`FullIO` holds all six sub-storages: [1](#0-0) 

`FullIOStateSnapshot` captures only five of them — `new_settlement_layer_chain_id_storage` is absent: [2](#0-1) 

`start_io_frame` snapshots five sub-storages and silently omits `new_settlement_layer_chain_id_storage`: [3](#0-2) 

`finish_io_frame` rolls back five sub-storages and silently omits `new_settlement_layer_chain_id_storage`: [4](#0-3) 

Yet `NewSettlementLayerChainIdStorage` was explicitly designed to be snapshottable — it has `start_frame()` and `finish_frame()` that are simply never called: [5](#0-4) 

The value is written when the `SettlementLayerChainIdUpdated` event is intercepted by the system-context event hook: [6](#0-5) 

At block finalization, `read_batch_context_inputs` asserts that the stored chain-ID matches the value actually committed to storage: [7](#0-6) 

If the transaction that emitted the event was rolled back, the storage write is undone (via the normal `storage` rollback path) but `new_settlement_layer_chain_id_storage` still holds the value. The assertion then compares a stale in-memory value against the rolled-back on-chain value and panics.

Additionally, `update()` guards against double-writes: [8](#0-7) 

A stale value left by a reverted transaction permanently blocks any subsequent legitimate chain-ID update in the same block.

---

### Impact Explanation

**State-transition / storage rollback bug.** Two concrete consequences:

1. **Block finalization panic.** If a transaction that emits `SettlementLayerChainIdUpdated` is rolled back (block-limit revert at `tx_loop.rs:161`, out-of-native revert, or explicit EVM revert), the `assert_eq!` in `read_batch_context_inputs` fires because `new_settlement_layer_chain_id_storage` holds a value that no longer matches the rolled-back storage slot. The block cannot be sealed; the state transition halts.

2. **Permanent DoS of chain-ID migration within a block.** The stale value causes every subsequent call to `update()` in the same block to return an `InternalError` ("Tried to update settlement layer chain id more than once in a block"), making it impossible to complete a legitimate settlement-layer chain-ID migration if the first attempt reverted.

Both outcomes corrupt the block-level state transition and are irreversible within the affected block.

---

### Likelihood Explanation

The trigger requires a transaction that:
- emits `SettlementLayerChainIdUpdated` (via the `SystemContext` contract or any contract if the hook is address-agnostic), **and**
- is subsequently rolled back by any of the existing rollback paths: block-gas/pubdata/native limit exceeded (`tx_loop.rs:161`), out-of-native (`FatalRuntimeError` path), or explicit EVM revert.

The block-limit rollback path is reachable by any transaction that pushes the block over its resource limits — an unprivileged sender can craft a gas-heavy transaction to force this. If the event hook is not restricted to the `SystemContext` address, any contract can emit the triggering event signature, making the attack fully unprivileged. Even if restricted to `SystemContext`, a governance upgrade transaction that reverts mid-execution (e.g., due to out-of-gas) triggers the same bug without any attacker involvement.

---

### Recommendation

Add `new_settlement_layer_chain_id_storage` to `FullIOStateSnapshot` and include it in both `start_io_frame` and `finish_io_frame`, mirroring the pattern already used for `interop_root_storage`:

```rust
// FullIOStateSnapshot
pub struct FullIOStateSnapshot<M: StorageModel> {
    io: M::StateSnapshot,
    transient: CacheSnapshotId,
    messages: usize,
    events: usize,
    interop_roots: usize,
    new_sl_chain_id: NewSettlementLayerChainIdSnapshotId, // ADD
}

// start_io_frame
let new_sl_chain_id = self.new_settlement_layer_chain_id_storage.start_frame(); // ADD
Ok(FullIOStateSnapshot { io, transient, messages, events, interop_roots, new_sl_chain_id })

// finish_io_frame
self.new_settlement_layer_chain_id_storage
    .finish_frame(rollback_handle.map(|x| x.new_sl_chain_id)); // ADD
```

---

### Proof of Concept

1. Block contains two transactions: Tx-A (chain-ID update) and Tx-B (gas-heavy filler).
2. Tx-A executes, emits `SettlementLayerChainIdUpdated`; the event hook writes `new_sl_chain_id = X` into `new_settlement_layer_chain_id_storage`.
3. Tx-B pushes the block over its gas limit; the bootloader rolls back Tx-A via `system.finish_global_frame(Some(&pre_tx_rollback_handle))` at `tx_loop.rs:161`.
4. `finish_io_frame` rolls back `storage` (the chain-ID slot reverts to its old value), `events_storage` (the event is removed), and all other sub-storages — but **not** `new_settlement_layer_chain_id_storage`, which still holds `X`.
5. Block finalization calls `read_batch_context_inputs`; `settlement_layer_chain_id` is read from storage (old value, not `X`); `new_settlement_layer_chain_id_storage.value()` returns `Some(X)`; `assert_eq!(X, old_value)` panics.
6. The block cannot be finalized; the state transition is broken. [9](#0-8) [10](#0-9) [7](#0-6)

### Citations

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L39-59)
```rust
pub struct FullIO<
    A: Allocator + Clone + Default,
    R: Resources,
    P: StorageAccessPolicy<R, Bytes32>,
    SF: StackFactory<N>,
    const N: usize,
    O: IOOracle,
    M: StorageModel<IOTypes = EthereumIOTypesConfig, Resources = R, InitData = P, Allocator = A>,
    const PROOF_ENV: bool,
> {
    pub storage: M,
    pub transient_storage: GenericTransientStorage<WarmStorageKey, Bytes32, SF, N, A>,
    pub logs_storage: LogsStorage<SF, N, A>,
    pub events_storage: EventsStorage<MAX_EVENT_TOPICS, SF, N, A>,
    pub interop_root_storage: InteropRootStorage<SF, N, A>,
    pub new_settlement_layer_chain_id_storage: NewSettlementLayerChainIdStorage<SF, N, A>,
    pub allocator: A,
    pub oracle: O,
    pub tx_number: u32,
    pub da_commitment_scheme: Option<DACommitmentScheme>,
}
```

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

**File:** zk_ee/src/common_structs/new_settlement_layer_chain_id_storage.rs (L37-62)
```rust
    pub fn start_frame(&mut self) -> NewSettlementLayerChainIdSnapshotId {
        self.history.snapshot()
    }

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

    pub fn value(&self) -> Option<&U256> {
        self.history.value()
    }

    #[track_caller]
    pub fn finish_frame(&mut self, rollback_handle: Option<NewSettlementLayerChainIdSnapshotId>) {
        if let Some(x) = rollback_handle {
            self.history.rollback(x);
        }
    }
```

**File:** system_hooks/src/event_hooks/system_context.rs (L60-65)
```rust
    let new_sl_chain_id = U256::from_be_bytes(topics[1].as_u8_array());
    system.io.update_settlement_layer_chain_id(
        ExecutionEnvironmentType::NoEE,
        resources,
        new_sl_chain_id,
    )?;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L234-238)
```rust
    if let Some(new_settlement_layer_chain_id) = io.new_settlement_layer_chain_id_storage.value() {
        // If the SL chain id was updated, make sure the updated one matches
        // the one read from storage.
        assert_eq!(new_settlement_layer_chain_id, &settlement_layer_chain_id);
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L119-122)
```rust
                            // Revert to state before transaction
                            system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                            result_keeper.tx_processed(Err(err));
                        }
```
