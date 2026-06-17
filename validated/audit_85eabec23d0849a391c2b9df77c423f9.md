The claim is concrete and traceable. Here is the analysis:

**Key constants:**

- `MAX_NUMBER_OF_LOGS = 16_384` — per-block limit enforced in `check_for_block_limits` [1](#0-0) 

- `logs_storage: ArrayVec<Bytes32, 16384>` — batch-level fixed-capacity accumulator in `ZKBatchDataKeeper` [2](#0-1) 

**The per-block guard:**

`check_for_block_limits` rejects a transaction only when `logs_used > MAX_NUMBER_OF_LOGS` (i.e., > 16384), so a single block may legally accumulate up to exactly 16384 logs. [3](#0-2) 

**The accumulation path in the proving run:**

After each block in a multiblock batch, `post_op` calls:

```rust
io.logs_storage.apply_to_array_vec(&mut batch_data.logs_storage);
``` [4](#0-3) 

`apply_to_array_vec` uses bare `push`, which **panics** when the `ArrayVec` is full:

```rust
pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
    self.list.iter().for_each(|el| {
        let log: L2ToL1Log = el.into();
        array_vec.push(log.hash())   // panics on overflow
    });
}
``` [5](#0-4) 

**The structural mismatch:**

| Layer | Limit |
|---|---|
| Per-block log limit (`check_for_block_limits`) | 16,384 |
| Batch-level `logs_storage` capacity (`ArrayVec`) | 16,384 |

In a multiblock batch with B blocks, the maximum total logs is `B × 16384`. For B ≥ 2, this can exceed 16384. There is **no batch-level log count guard** anywhere in the code.

**Forward run vs. proving run divergence:**

The forward run stores logs in `LogsStorage`, which is backed by a `HistoryList` (heap-allocated, unbounded): [6](#0-5) 

It never panics. The proving run uses the fixed-capacity `ArrayVec<Bytes32, 16384>` and panics when the second block's logs are appended. This creates a **forward-run-succeeds / proving-run-panics** split for any valid multiblock batch whose total log count exceeds 16384.

**Verdict:**

---

### Title
`ZKBatchDataKeeper.logs_storage` ArrayVec Overflow in Multiblock Batch Proving Run — (`basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs`, `zk_ee/src/common_structs/logs_storage.rs`)

### Summary
In multiblock batch proving mode, each block's L2→L1 log hashes are appended to a fixed-capacity `ArrayVec<Bytes32, 16384>`. The per-block log limit is also 16384, so two blocks each emitting the maximum number of logs produce 32768 total entries — double the ArrayVec capacity. `apply_to_array_vec` uses bare `push`, which panics on overflow. The forward run uses an unbounded heap structure and succeeds. The result is a valid batch that is permanently unprovable.

### Finding Description
`ZKBatchDataKeeper.logs_storage` is declared as `ArrayVec<Bytes32, 16384>` — a fixed-capacity stack-allocated vector. In `post_tx_op_proving_multiblock_batch.rs`, after each block is processed, `io.logs_storage.apply_to_array_vec(&mut batch_data.logs_storage)` appends that block's log hashes into this shared accumulator. `apply_to_array_vec` calls `array_vec.push(log.hash())` for every log without checking remaining capacity. `ArrayVec::push` panics when the vector is full.

The per-block enforcement in `check_for_block_limits` allows up to 16384 logs per block. There is no batch-level log count limit. For a multiblock batch with two blocks each carrying 8193 logs (total 16386 > 16384), the second call to `apply_to_array_vec` panics in the proving run. The forward run is unaffected because it uses `LogsStorage` backed by a heap-allocated `HistoryList`.

### Impact Explanation
Any valid multiblock batch whose total L2→L1 log count across all blocks exceeds 16384 becomes permanently unprovable. The proving run panics, the batch cannot be finalized on L1, and the chain halts at that batch. This matches the scoped impact: **valid-but-unprovable execution**.

### Likelihood Explanation
An unprivileged user can emit L2→L1 logs via the L1 messenger system hook from any contract. Filling two blocks to just over half the per-block log limit (8193 logs each) is sufficient. Whether this is gas-feasible depends on the per-log gas cost, but the structural invariant violation exists regardless of gas pricing and could be triggered deliberately or accidentally.

### Recommendation
Enforce a batch-level log limit. Either:
1. Reduce the per-block log limit to `16384 / max_blocks_per_batch`, or
2. Track cumulative log count in `ZKBatchDataKeeper` and reject blocks that would push the batch total over 16384, or
3. Replace `apply_to_array_vec`'s bare `push` with `try_push` and propagate an error instead of panicking.

The root invariant to enforce: `MAX_NUMBER_OF_LOGS * max_blocks_per_batch ≤ 16384`.

### Proof of Concept
1. In multiblock batch proving mode, submit block 1 with 8193 L2→L1 logs (via L1 messenger calls from a contract loop). `check_for_block_limits` passes (8193 ≤ 16384). `apply_to_array_vec` appends 8193 hashes; `batch_data.logs_storage.len() == 8193`.
2. Submit block 2 with 8193 L2→L1 logs. `check_for_block_limits` passes again. `apply_to_array_vec` attempts to append 8193 more hashes. At the 8192nd push (total 16385), `ArrayVec::push` panics: capacity exceeded.
3. The forward run processes both blocks without issue (unbounded `HistoryList`).
4. The batch is valid per the forward run but the proving run panics — the batch is unprovable.

### Citations

**File:** zk_ee/src/common_structs/logs_storage.rs (L25-25)
```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L157-161)
```rust
pub struct LogsStorage<SF: StackFactory<M>, const M: usize, A: Allocator + Clone = Global> {
    list: HistoryList<LogContent<A>, u32, SF, M, A>,
    pubdata_used_by_committed_logs: u32,
    _marker: core::marker::PhantomData<A>,
}
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L27-27)
```rust
    pub logs_storage: ArrayVec<Bytes32, 16384>,
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L109-110)
```rust
        io.logs_storage
            .apply_to_array_vec(&mut batch_data.logs_storage);
```
