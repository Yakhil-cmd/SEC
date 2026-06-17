### Title
`new_settlement_layer_chain_id_storage` Not Rolled Back on Frame Revert — (`basic_system/src/system_implementation/system/io_subsystem.rs`)

---

### Summary

`FullIOStateSnapshot` omits `new_settlement_layer_chain_id_storage` from its frame-snapshot fields. As a result, `finish_io_frame` never rolls back this storage when a transaction reverts. A transaction that emits `SettlementLayerChainIdUpdated` and then reverts (e.g., due to block-limit overflow) leaves `new_settlement_layer_chain_id_storage` permanently set to the new chain ID while the underlying `SystemContext` storage slot is correctly rolled back. The resulting inconsistency triggers a hard `assert_eq!` panic in `read_batch_context_inputs`, halting block finalization.

---

### Finding Description

**Root cause — missing snapshot field and missing rollback call**

`FullIO` holds six sub-storages. Five of them are snapshotted and rolled back; `new_settlement_layer_chain_id_storage` is not.

`FullIOStateSnapshot` definition:

```rust
pub struct FullIOStateSnapshot<M: StorageModel> {
    io: M::StateSnapshot,
    transient: CacheSnapshotId,
    messages: usize,
    events: usize,
    interop_roots: usize,
    // new_settlement_layer_chain_id is ABSENT
}
``` [1](#0-0) 

`start_io_frame` takes a snapshot of every sub-storage **except** `new_settlement_layer_chain_id_storage`:

```rust
fn start_io_frame(&mut self) -> Result<Self::StateSnapshot, InternalError> {
    let io = self.storage.start_frame();
    let transient = self.transient_storage.start_frame();
    let messages = self.logs_storage.start_frame();
    let events = self.events_storage.start_frame();
    let interop_roots = self.interop_root_storage.start_frame();
    // self.new_settlement_layer_chain_id_storage.start_frame() — NEVER CALLED
    Ok(FullIOStateSnapshot { io, transient, messages, events, interop_roots })
}
``` [2](#0-1) 

`finish_io_frame` rolls back every sub-storage **except** `new_settlement_layer_chain_id_storage`:

```rust
fn finish_io_frame(&mut self, rollback_handle: Option<&Self::StateSnapshot>) -> Result<(), InternalError> {
    self.storage.finish_frame(rollback_handle.map(|x| &x.io))?;
    self.transient_storage.finish_frame(rollback_handle.map(|x| &x.transient))?;
    self.logs_storage.finish_frame(rollback_handle.map(|x| x.messages));
    self.events_storage.finish_frame(rollback_handle.map(|x| x.events));
    self.interop_root_storage.finish_frame(rollback_handle.map(|x| x.interop_roots));
    // self.new_settlement_layer_chain_id_storage.finish_frame(...) — NEVER CALLED
    Ok(())
}
``` [3](#0-2) 

Yet `NewSettlementLayerChainIdStorage` **does** expose both `start_frame()` and `finish_frame()` — they are simply never wired in: [4](#0-3) 

**How the storage is written**

The `system_context_event_hook` intercepts any `SettlementLayerChainIdUpdated(uint256)` event emitted by `SYSTEM_CONTEXT_ADDRESS` and calls `update_settlement_layer_chain_id`, which writes into `new_settlement_layer_chain_id_storage`: [5](#0-4) [6](#0-5) 

This write happens **inside** the execution frame opened by `start_global_frame`. When the frame is later rolled back (e.g., block-limit overflow), the persistent storage slot in `SystemContext` is correctly reverted, but `new_settlement_layer_chain_id_storage` retains the new value.

**Downstream assertion failure**

`read_batch_context_inputs` (called during block finalization) asserts that the two values agree:

```rust
if let Some(new_settlement_layer_chain_id) = io.new_settlement_layer_chain_id_storage.value() {
    assert_eq!(new_settlement_layer_chain_id, &settlement_layer_chain_id);
}
``` [7](#0-6) 

After a reverted chain-ID update, `new_settlement_layer_chain_id_storage` holds the new ID while `settlement_layer_chain_id` (read from the rolled-back storage slot) holds the old ID. The `assert_eq!` panics, halting block finalization.

**Secondary effect — "already updated" guard fires on retry**

`NewSettlementLayerChainIdStorage::update` guards against a second write in the same block:

```rust
pub fn update(&mut self, new_sl_chain_id: U256) -> Result<(), SystemError> {
    if self.value().is_some() {
        return Err(internal_error!(
            "Tried to update settlement layer chain id more than once in a block"
        ).into());
    }
    ...
}
``` [8](#0-7) 

Because the reverted transaction left `value()` as `Some(...)`, any subsequent legitimate service transaction that tries to set the chain ID in the same block will hit this guard and fail with an internal error.

---

### Impact Explanation

1. **Block finalization halted (panic):** The `assert_eq!` in `read_batch_context_inputs` panics, making it impossible to finalize the block. This is a liveness / denial-of-service impact at the block level.
2. **Settlement-layer chain-ID update permanently blocked for the block:** The "already updated" guard prevents any further chain-ID update in the same block, even though the first one was reverted.
3. **State divergence between forward and proving paths:** The `new_settlement_layer_chain_id_storage` value is used in batch output construction (`settlement_layer_chain_id` field of `BatchOutput`). A stale value here would cause the forward and proving executions to produce different public inputs, breaking proof validity. [9](#0-8) 

---

### Likelihood Explanation

The trigger condition is: a service transaction that calls `SystemContext.setSettlementLayerChainId` succeeds in emitting the event (so the hook fires and writes `new_settlement_layer_chain_id_storage`) but is then reverted by the block-limit check in the ZK transaction loop:

```rust
if let Err(err) = check_for_block_limits(...) {
    system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
    result_keeper.tx_processed(Err(err));
}
``` [10](#0-9) 

Service transactions are operator-controlled, but the block-limit revert path is reachable without any special privilege — the operator simply needs to include a chain-ID update service transaction in a block that is already near its gas/pubdata/log limits. An adversary who can influence block contents (e.g., by flooding the mempool to push the block to its limits before the service transaction) can force this condition.

---

### Recommendation

Add `new_settlement_layer_chain_id_storage` to `FullIOStateSnapshot` and wire its `start_frame` / `finish_frame` calls into `start_io_frame` / `finish_io_frame`, mirroring the existing pattern for `interop_root_storage`:

```rust
pub struct FullIOStateSnapshot<M: StorageModel> {
    io: M::StateSnapshot,
    transient: CacheSnapshotId,
    messages: usize,
    events: usize,
    interop_roots: usize,
    new_sl_chain_id: NewSettlementLayerChainIdSnapshotId, // ADD
}

fn start_io_frame(&mut self) -> Result<Self::StateSnapshot, InternalError> {
    ...
    let new_sl_chain_id = self.new_settlement_layer_chain_id_storage.start_frame(); // ADD
    Ok(FullIOStateSnapshot { io, transient, messages, events, interop_roots, new_sl_chain_id })
}

fn finish_io_frame(&mut self, rollback_handle: Option<&Self::StateSnapshot>) -> Result<(), InternalError> {
    ...
    self.new_settlement_layer_chain_id_storage                                       // ADD
        .finish_frame(rollback_handle.map(|x| x.new_sl_chain_id));
    Ok(())
}
```

---

### Proof of Concept

1. Deploy a block that is near its gas/pubdata limit.
2. Include a service transaction calling `SystemContext.setSettlementLayerChainId(newId)`.
3. During execution the `SettlementLayerChainIdUpdated(newId)` event fires; `system_context_event_hook` calls `update_settlement_layer_chain_id(newId)`, writing `new_settlement_layer_chain_id_storage`.
4. The block-limit check (`check_for_block_limits`) detects overflow and calls `system.finish_global_frame(Some(&pre_tx_rollback_handle))`.
5. `finish_io_frame` rolls back `storage` (reverting the `SystemContext` slot-0 write) but **does not** roll back `new_settlement_layer_chain_id_storage`.
6. Block finalization calls `read_batch_context_inputs`:
   - `io.new_settlement_layer_chain_id_storage.value()` → `Some(newId)`
   - `read_settlement_layer_chain_id(io)` reads slot 0 of `SystemContext` → `oldId`
   - `assert_eq!(newId, oldId)` → **PANIC**, block finalization aborted.

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

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L402-416)
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
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L418-433)
```rust
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L101-102)
```rust
        hasher.update(self.settlement_layer_chain_id.to_be_bytes::<32>());
        hasher.finalize()
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L152-162)
```rust
                            if let Err(err) = check_for_block_limits(
                                system,
                                next_block_gas_used,
                                next_block_computational_native_used,
                                next_block_pubdata_used,
                                block_logs_used,
                                next_block_blob_gas_used,
                            ) {
                                // Revert to state before transaction
                                system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                                result_keeper.tx_processed(Err(err));
```
