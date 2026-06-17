### Title
Zero Hash Used as Sentinel Allows Upgrade Transaction with Hash `Bytes32::ZERO` to Bypass Duplicate Detection and Corrupt `number_of_layer_2_txs` in Batch Public Input — (`File: basic_bootloader/src/bootloader/block_flow/zk/block_data.rs`)

---

### Summary

`UpgradeTx` uses `Bytes32::ZERO` as a sentinel value to indicate "no upgrade transaction recorded." However, if an upgrade transaction's computed hash happens to be `Bytes32::ZERO`, the duplicate-detection guard silently passes, the hash is stored as zero, and downstream logic (`is_empty()`, `has_upgrade_tx()`) treats the recorded upgrade as absent. This causes `number_of_layer_2_txs` in the batch public input to be miscounted, producing an incorrect proof public input committed to the settlement layer.

---

### Finding Description

In `basic_bootloader/src/bootloader/block_flow/zk/block_data.rs`, the `UpgradeTx` struct uses `Bytes32::ZERO` as both the initial "empty" state and as the sentinel for "no upgrade tx recorded":

```rust
pub struct UpgradeTx {
    inner: Bytes32,
}

impl UpgradeTx {
    pub fn add_upgrade_tx_hash(&mut self, tx_hash: &Bytes32) {
        if self.inner.is_zero() == false {   // ← sentinel check
            panic!("duplicate upgrade tx");
        }
        self.inner = *tx_hash;               // ← stores zero if tx_hash == ZERO
    }

    pub fn is_empty(&self) -> bool {
        self.inner.is_zero()                 // ← ambiguous: "no tx" OR "tx with zero hash"
    }
}
``` [1](#0-0) 

If an upgrade transaction's hash is `Bytes32::ZERO`, `add_upgrade_tx_hash` does not panic (the guard passes), stores zero, and `is_empty()` returns `true`. The same zero-sentinel ambiguity exists in `ZKBatchDataKeeper::has_upgrade_tx()`:

```rust
pub fn has_upgrade_tx(&self) -> bool {
    self.upgrade_tx_hash
        .is_some_and(|hash| hash != Bytes32::ZERO)  // ← zero hash treated as "no upgrade"
}
``` [2](#0-1) 

In `post_tx_op_proving_singleblock_batch.rs`, `is_empty()` controls whether `number_of_layer_2_txs` is decremented:

```rust
if !block_data.upgrade_tx_recorder.is_empty() {
    number_of_layer_2_txs -= 1;
}
``` [3](#0-2) 

If the upgrade tx hash is zero, `is_empty()` returns `true`, the decrement is skipped, and `number_of_layer_2_txs` is overcounted by 1. The same logic applies in the multiblock batch path via `ZKBatchDataKeeper::has_upgrade_tx()`. [4](#0-3) 

The `upgrade_tx_hash` field is also written directly into `BatchOutput` and hashed into the batch public input committed on the settlement layer:

```rust
upgrade_tx_hash: self.upgrade_tx_hash.unwrap(),
``` [5](#0-4) 

The `BatchOutput` is hashed into the final `BatchPublicInput` that is verified on L1: [6](#0-5) 

Additionally, the duplicate-upgrade guard in `UpgradeTx::add_upgrade_tx_hash` uses `is_zero()` as the "not yet set" check. If a zero-hash upgrade tx is recorded, a second upgrade tx can be submitted in the same block without triggering the `panic!("duplicate upgrade tx")` guard, because `self.inner.is_zero()` is still `true` after storing the first zero hash. [7](#0-6) 

---

### Impact Explanation

1. **Incorrect batch public input committed to L1**: `number_of_layer_2_txs` is overcounted by 1 in the `BatchOutput` hash. The settlement layer verifies this value; a mismatch between the on-chain expectation and the proven value can cause batch rejection or acceptance of a fraudulent batch.
2. **Duplicate upgrade transaction bypass**: A second upgrade transaction in the same block is not detected if the first had a zero hash, violating the invariant that at most one upgrade tx is allowed per block.
3. **Incorrect `upgrade_tx_hash` in public input**: The zero hash is committed as `upgrade_tx_hash` in `BatchOutput`, which the settlement layer interprets as "no upgrade occurred," even though an upgrade transaction was actually executed.

---

### Likelihood Explanation

The transaction hash is computed as a Keccak256 hash of the ABI-encoded upgrade transaction fields. The probability of a naturally occurring zero hash is negligible (~2^-256). However, this is a **protocol-level invariant violation** that can be triggered by a crafted upgrade transaction whose hash is engineered to be zero (a preimage attack on Keccak256, which is currently infeasible), or more practically, by a bug in the hash computation path that produces zero for certain inputs. The duplicate-upgrade bypass is the more realistic concern: if any code path sets `inner` to zero (e.g., via a reset or re-initialization), the guard fails silently.

---

### Recommendation

Replace the zero-sentinel pattern with an explicit `Option<Bytes32>`:

```rust
pub struct UpgradeTx {
    inner: Option<Bytes32>,
}

impl UpgradeTx {
    pub fn add_upgrade_tx_hash(&mut self, tx_hash: &Bytes32) {
        if self.inner.is_some() {
            panic!("duplicate upgrade tx");
        }
        self.inner = Some(*tx_hash);
    }

    pub fn is_empty(&self) -> bool {
        self.inner.is_none()
    }

    pub fn finish(self) -> Bytes32 {
        self.inner.unwrap_or(Bytes32::ZERO)
    }
}
```

Similarly, `ZKBatchDataKeeper::has_upgrade_tx()` should check `self.upgrade_tx_hash.is_some()` rather than comparing the hash value to zero.

---

### Proof of Concept

1. Craft an upgrade transaction whose ABI-encoded hash computes to `Bytes32::ZERO` (or simulate this by directly setting `tx_hash = Bytes32::ZERO` in a test).
2. Submit it as the first transaction in a block.
3. `add_upgrade_tx_hash` stores `Bytes32::ZERO` without panicking.
4. `is_empty()` returns `true` → `number_of_layer_2_txs` is not decremented → overcounted by 1.
5. `has_upgrade_tx()` returns `false` → same overcounting in multiblock batch path.
6. The `BatchOutput` hash committed to L1 contains an incorrect `number_of_layer_2_txs` and `upgrade_tx_hash = Bytes32::ZERO` (indistinguishable from "no upgrade").
7. Submit a second upgrade transaction in the same block: `add_upgrade_tx_hash` is called again; `self.inner.is_zero()` is still `true`, so the duplicate guard does not fire, and the second upgrade tx hash overwrites the first.

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/block_data.rs (L141-165)
```rust
pub struct UpgradeTx {
    inner: Bytes32,
}

impl UpgradeTx {
    /// Records the hash of an upgrade transaction.
    ///
    /// Panics if an upgrade transaction was already recorded for this block.
    /// ZKsync allows at most one upgrade transaction per block.
    pub fn add_upgrade_tx_hash(&mut self, tx_hash: &Bytes32) {
        if self.inner.is_zero() == false {
            panic!("duplicate upgrade tx");
        }
        self.inner = *tx_hash;
    }

    /// Returns the upgrade transaction hash, or zero if no upgrade occurred.
    pub fn finish(self) -> Bytes32 {
        self.inner
    }

    /// Returns if an upgrade transaction has been recorded
    pub fn is_empty(&self) -> bool {
        self.inner.is_zero()
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L113-116)
```rust
    pub fn has_upgrade_tx(&self) -> bool {
        self.upgrade_tx_hash
            .is_some_and(|hash| hash != Bytes32::ZERO)
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L135-138)
```rust
        let mut number_of_layer_2_txs = self.tx_count - number_of_layer_1_txs;
        if has_upgrade_tx {
            number_of_layer_2_txs -= U256::ONE;
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L149-149)
```rust
            upgrade_tx_hash: self.upgrade_tx_hash.unwrap(),
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L110-112)
```rust
        if !block_data.upgrade_tx_recorder.is_empty() {
            number_of_layer_2_txs -= 1;
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L52-80)
```rust
pub struct BatchOutput {
    /// Chain id used during execution of the blocks.
    pub chain_id: U256,
    /// First block timestamp.
    pub first_block_timestamp: u64,
    /// Last block timestamp.
    pub last_block_timestamp: u64,
    /// DA commitment scheme.
    pub da_commitment_scheme: DACommitmentScheme,
    /// Pubdata commitment.
    pub pubdata_commitment: Bytes32,
    /// Number of l1 -> l2 processed txs in the batch.
    pub number_of_layer_1_txs: U256,
    /// Number of processed L2 txs in the batch.
    pub number_of_layer_2_txs: U256,
    /// Rolling keccak256 hash of l1 -> l2 txs processed in the batch.
    pub priority_operations_hash: Bytes32,
    /// L2 logs tree root.
    /// Note that it's full root, it's keccak256 of:
    /// - merkle root of l2 -> l1 logs in the batch .
    /// - multichain root - commitment to logs emitted on chains that settle on the current.
    pub l2_logs_tree_root: Bytes32,
    /// Protocol upgrade tx hash (0 if there wasn't)
    pub upgrade_tx_hash: Bytes32,
    /// Linear keccak256 hash of interop roots
    pub interop_roots_rolling_hash: Bytes32,
    /// Settlement layer chain id.
    pub settlement_layer_chain_id: U256,
}
```
