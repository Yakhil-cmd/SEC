### Title
`ZKBatchDataKeeper.logs_storage` Fixed-Size `ArrayVec` Panics When Multi-Block Batch Exceeds 16,384 Total L2→L1 Logs - (`basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs`)

---

### Summary

`ZKBatchDataKeeper` accumulates L2→L1 log hashes across all blocks in a multi-block batch into a fixed-capacity `ArrayVec<Bytes32, 16384>`. The per-block log limit is also 16,384. When a batch contains two or more blocks whose combined log count exceeds 16,384, the `ArrayVec::push` call in `apply_to_array_vec` panics, halting the prover/sequencer.

---

### Finding Description

`ZKBatchDataKeeper` holds a batch-level log accumulator:

```rust
pub logs_storage: ArrayVec<Bytes32, 16384>,
``` [1](#0-0) 

This field is populated per-block via `LogsStorage::apply_to_array_vec`, which unconditionally calls `ArrayVec::push` for every log in the block:

```rust
pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
    self.list.iter().for_each(|el| {
        let log: L2ToL1Log = el.into();
        array_vec.push(log.hash())   // panics if full
    });
}
``` [2](#0-1) 

The per-block enforcement in `check_for_block_limits` rejects any single block that exceeds `MAX_NUMBER_OF_LOGS = 16_384`: [3](#0-2) [4](#0-3) 

However, the batch-level `logs_storage` has the **same** capacity (16,384) as the per-block limit, yet it accumulates logs from **all** blocks in the batch. For a batch of N blocks, the maximum total logs is N × 16,384. There is no guard before `push` to prevent overflow. Rust's `arrayvec::ArrayVec::push` panics when the array is at capacity.

The multiblock batch post-tx-op calls `apply_to_array_vec` on `batch_data.logs_storage` for each block: [5](#0-4) 

(confirmed by grep: `apply_to_array_vec` is called only in `post_tx_op_proving_multiblock_batch.rs`)

The `l2_logs_root` function that consumes `logs_storage` to compute the batch Merkle root is only reached after all blocks have been accumulated: [6](#0-5) 

---

### Impact Explanation

When a multi-block batch's total L2→L1 log count exceeds 16,384, `apply_to_array_vec` panics. This aborts the prover/sequencer process mid-batch, preventing the batch public input from being computed and submitted to L1. The chain halts at that batch boundary until the batch is re-constructed with fewer logs per block. This is a **liveness/valid-execution unprovability** impact: a sequence of individually valid blocks cannot be proven together as a batch.

---

### Likelihood Explanation

Any unprivileged user can emit L2→L1 logs via the L1 Messenger system hook (`sendToL1`). A batch of just 2 blocks, each with 8,193 logs (well within the per-block limit of 16,384), produces 16,386 total logs — enough to trigger the panic. An attacker can deliberately craft transactions to fill log capacity across multiple blocks in a batch, reliably triggering the panic whenever the operator attempts to prove a multi-block batch.

---

### Recommendation

Replace the unchecked `ArrayVec::push` in `apply_to_array_vec` with `try_push` and propagate an error, or enforce a batch-level log cap that accounts for the number of blocks in the batch (e.g., `capacity = MAX_NUMBER_OF_LOGS * max_blocks_per_batch`). The simplest fix is to use a dynamically-sized `Vec` for `ZKBatchDataKeeper.logs_storage` instead of a fixed-capacity `ArrayVec<Bytes32, 16384>`, since the Merkle tree computation in `l2_logs_root` already handles arbitrary sizes up to 16,384 leaves per block.

---

### Proof of Concept

1. Operator opens a multi-block batch (2 blocks).
2. Attacker submits transactions in Block 1 that each call `L1Messenger.sendToL1(...)`, emitting 8,193 L2→L1 logs total (within the per-block limit of 16,384).
3. Attacker submits transactions in Block 2 that emit another 8,193 L2→L1 logs.
4. Both blocks are individually valid and pass `check_for_block_limits`.
5. When the prover calls `apply_to_array_vec` for Block 2's logs into `batch_data.logs_storage`, the 8,194th `push` call panics because the `ArrayVec<Bytes32, 16384>` is already full from Block 1's 8,193 entries.
6. The prover process aborts; the batch cannot be finalized or submitted to L1. [1](#0-0) [7](#0-6) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L27-27)
```rust
    pub logs_storage: ArrayVec<Bytes32, 16384>,
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L126-126)
```rust
        chain_batch_root_hasher.update(Self::l2_logs_root(self.logs_storage).as_u8_ref());
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L24-25)
```rust
// Taken from the size of the Merkle tree.
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L1-1)
```rust
use super::*;
```
