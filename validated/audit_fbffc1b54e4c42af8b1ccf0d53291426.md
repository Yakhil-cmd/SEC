### Title
Multi-Block Batch L2→L1 Log Accumulation Overflow Causes Unprovable Batch — (`basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs`, `zk_ee/src/common_structs/logs_storage.rs`)

---

### Summary

In the multi-block batch proving path, `ZKBatchDataKeeper.logs_storage` is a fixed-capacity `ArrayVec<Bytes32, 16384>`. Each block's logs are appended into this array without any batch-level capacity guard. Because the per-block log limit equals the array's total capacity, a batch containing two or more blocks that together emit more than 16,384 L2→L1 logs will cause `ArrayVec::push` to panic inside `apply_to_array_vec`, making the entire batch unprovable. Users whose L2→L1 messages fall in that batch cannot have them proven and relayed to L1.

---

### Finding Description

`ZKBatchDataKeeper` accumulates L2→L1 log hashes from every block in a multi-block batch into a single `ArrayVec<Bytes32, 16384>`:

```rust
pub logs_storage: ArrayVec<Bytes32, 16384>,
``` [1](#0-0) 

The capacity constant `16384` is identical to `MAX_NUMBER_OF_LOGS`, the per-block limit:

```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
``` [2](#0-1) 

At the end of each block in the proving path, `apply_to_array_vec` is called unconditionally:

```rust
io.logs_storage
    .apply_to_array_vec(&mut batch_data.logs_storage);
``` [3](#0-2) 

`apply_to_array_vec` calls `array_vec.push(log.hash())` for every log in the block:

```rust
pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
    self.list.iter().for_each(|el| {
        let log: L2ToL1Log = el.into();
        array_vec.push(log.hash())
    });
}
``` [4](#0-3) 

`ArrayVec::push` panics when the array is full. The per-block limit enforced by `check_for_block_limits` only guards individual blocks:

```rust
} else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
    Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
``` [5](#0-4) 

There is no batch-level guard. A batch of N blocks can legally accumulate up to N × 16,384 logs, but the accumulator only holds 16,384. The `into_public_input` function then computes the `l2_logs_tree_root` from this array:

```rust
chain_batch_root_hasher.update(Self::l2_logs_root(self.logs_storage).as_u8_ref());
``` [6](#0-5) 

If the panic occurs before this point, the batch public input is never produced.

---

### Impact Explanation

When the prover executes `ZKHeaderStructurePostTxOpProvingMultiblockBatch::post_op` for the second (or later) block that pushes the cumulative log count past 16,384, the RISC-V prover panics and cannot generate a ZK proof for the batch. The batch is permanently stuck: it cannot be submitted to the settlement layer. All L2→L1 messages (user withdrawals, cross-chain messages) contained in that batch are undeliverable on L1. This is a direct loss of user funds / message delivery for every user who sent an L2→L1 message in the affected batch.

---

### Likelihood Explanation

Any unprivileged user can emit L2→L1 logs by calling the L1Messenger system hook (`sendToL1`). A single block can hold up to 16,384 logs. A multi-block batch with just two blocks, each carrying 8,193 logs (well within the per-block limit), totals 16,386 — two over the batch array capacity. This is reachable under normal high-throughput conditions or by a deliberate attacker who sends enough L1Messenger calls across two consecutive blocks that are batched together.

---

### Recommendation

Add a batch-level log count check before calling `apply_to_array_vec`. Either:

1. Increase `logs_storage` capacity to `MAX_NUMBER_OF_LOGS * MAX_BLOCKS_PER_BATCH`, or
2. Enforce a batch-level log limit and reject (or split) blocks that would overflow the accumulator, or
3. Replace `ArrayVec::push` with `try_push` and propagate the error as a hard batch-sealing condition so the prover does not panic.

The per-block limit alone is insufficient; a corresponding batch-level limit must be enforced before the proving path is entered.

---

### Proof of Concept

1. Sequencer produces a multi-block batch with two blocks:
   - Block 1: transactions emit exactly 8,193 L2→L1 logs via `sendToL1`. Per-block limit (16,384) is not exceeded; block is accepted.
   - Block 2: transactions emit exactly 8,193 L2→L1 logs. Per-block limit is not exceeded; block is accepted.
2. Prover enters `ZKHeaderStructurePostTxOpProvingMultiblockBatch::post_op` for Block 1. `apply_to_array_vec` pushes 8,193 hashes into `batch_data.logs_storage` (capacity 16,384). No panic.
3. Prover enters `post_op` for Block 2. `apply_to_array_vec` attempts to push 8,193 more hashes. After the 8,192nd push the array is full (8,193 + 8,192 = 16,385 > 16,384). The 16,385th `ArrayVec::push` panics.
4. The RISC-V prover aborts. No proof is generated. The batch is permanently unprovable. All L2→L1 messages from both blocks are undeliverable on L1.

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L27-27)
```rust
    pub logs_storage: ArrayVec<Bytes32, 16384>,
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L126-126)
```rust
        chain_batch_root_hasher.update(Self::l2_logs_root(self.logs_storage).as_u8_ref());
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L25-25)
```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L311-316)
```rust
    pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
        self.list.iter().for_each(|el| {
            let log: L2ToL1Log = el.into();
            array_vec.push(log.hash())
        });
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L109-110)
```rust
        io.logs_storage
            .apply_to_array_vec(&mut batch_data.logs_storage);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L84-90)
```rust
    } else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block logs limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
```
