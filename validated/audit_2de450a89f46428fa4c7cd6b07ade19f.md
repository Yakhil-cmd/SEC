### Title
Unbounded `pending_updated_elements` Growth in `calculate_pubdata_used_by_tx` Causes Quadratic Resource Accounting Cost Per Transaction - (`File: basic_system/src/system_implementation/flat_storage_model/storage_cache.rs`)

---

### Summary

The `calculate_pubdata_used_by_tx` function in ZKsync OS iterates over `pending_updated_elements` — a list that accumulates every storage write within the current transaction, including repeated writes to the same slot. Because a single EVM transaction can write to the same storage slot an unbounded number of times (e.g., via a loop), this list grows proportionally to the number of SSTORE operations, not the number of unique slots. The function is called multiple times per transaction (at post-execution pubdata check and at refund), so the cost of iterating this list is paid repeatedly and grows with the number of writes. An attacker can craft a transaction that performs a large number of SSTORE operations to the same slot, making each subsequent call to `calculate_pubdata_used_by_tx` proportionally more expensive in native cycles, causing resource accounting divergence between the gas charged and the actual proving cost.

---

### Finding Description

`calculate_pubdata_used_by_tx` in `NewStorageWithAccountPropertiesUnderHash` iterates `self.0.cache.iter_altered_since_commit()`:

```rust
// basic_system/src/system_implementation/flat_storage_model/storage_cache.rs:265-308
pub fn calculate_pubdata_used_by_tx(&self) -> u32 {
    let mut visited_elements = BTreeSet::new_in(self.0.alloc.clone());
    let mut pubdata_used = 0u32;
    for element_history in self.0.cache.iter_altered_since_commit() {
        ...
        if visited_elements.contains(element_key) { continue; }
        visited_elements.insert(element_key);
        ...
    }
    pubdata_used
}
```

`iter_altered_since_commit` iterates `pending_updated_elements`:

```rust
// zk_ee/src/common_structs/history_map/mod.rs:251-264
pub fn iter_altered_since_commit(&'_ self) -> impl Iterator<...> {
    self.state.pending_updated_elements.iter()
        .map(|(k, _)| HistoryMapItemRef { ... })
}
```

`pending_updated_elements` is a `StackLinkedList` that is appended to on every write to a storage slot, even repeated writes to the same key. It is only cleared on `commit()` (called at `begin_new_tx`), not on `snapshot()`/`rollback()`. Within a single transaction, every SSTORE to any slot — including repeated writes to the same slot — appends a new entry.

The same pattern exists in `FlatStorageModelAccountCache::calculate_pubdata_used_by_tx`:

```rust
// basic_system/src/system_implementation/flat_storage_model/account_cache.rs:437
for element_history in self.cache.iter_altered_since_commit() {
```

`calculate_pubdata_used_by_tx` is called via `net_pubdata_used` → `pubdata_used_by_tx` at least twice per transaction:
1. In `check_enough_resources_for_pubdata` after execution body (`execute_or_deploy_inner`)
2. In `get_resources_to_charge_for_pubdata` during the refund step (`before_refund`)

Each call iterates the full `pending_updated_elements` list. A transaction that performs N SSTORE operations (even to the same slot) causes O(N) work per call, and O(N) total calls to `calculate_pubdata_used_by_tx` means O(N) total iteration work per transaction.

---

### Impact Explanation

**Resource accounting bug / valid-execution unprovability**: The native cycle cost of `calculate_pubdata_used_by_tx` is not charged to the transaction's gas. A transaction that performs many SSTORE operations to the same slot pays EVM gas for those SSTOREs (which is bounded by the gas limit), but the native proving cost of iterating `pending_updated_elements` is not reflected in the gas charged. This creates a divergence between gas consumed and native cycles consumed.

In the proving (RISC-V) context, this means a transaction can consume far more native cycles than its gas limit implies, potentially causing the prover to exceed its native cycle budget (`MAX_NATIVE_COMPUTATIONAL`) for a block, making a valid block unprovable. In the sequencer context, it causes unbounded CPU work per transaction that is not gas-metered.

---

### Likelihood Explanation

Any unprivileged EVM transaction sender can trigger this by deploying or calling a contract that executes a loop of SSTORE operations to the same storage slot. The EVM gas cost for repeated warm SSTOREs (no-op writes, same value) is only 100 gas each (warm SSTORE cost), so an attacker can pack thousands of SSTORE operations into a single transaction within the block gas limit. Each such operation appends to `pending_updated_elements` without being deduplicated, making the iteration cost of `calculate_pubdata_used_by_tx` grow linearly with the number of SSTORE opcodes executed.

---

### Recommendation

Deduplicate `pending_updated_elements` at insertion time, or replace the list with a set so that repeated writes to the same key do not add duplicate entries. Alternatively, maintain a separate counter of unique-key writes since last commit, and use that for the pubdata calculation loop rather than iterating the full write history. The `visited_elements` deduplication inside `calculate_pubdata_used_by_tx` already handles the output correctly, but the iteration cost is still O(total writes), not O(unique keys).

---

### Proof of Concept

1. Attacker deploys a contract with a loop:
   ```solidity
   fallback() external {
       for (uint256 i = 0; i < 10000; i++) {
           assembly { sstore(0, 1) }  // repeated warm SSTORE to slot 0
       }
   }
   ```
2. Each `sstore(0, 1)` costs 100 gas (warm, no-op write). With a 30M gas block limit, ~300,000 such operations fit in one transaction.
3. Each SSTORE appends to `pending_updated_elements` in `HistoryMap`.
4. After execution, `check_enough_resources_for_pubdata` calls `net_pubdata_used` → `pubdata_used_by_tx` → `calculate_pubdata_used_by_tx`, which iterates all 300,000 entries.
5. During `before_refund`, `get_resources_to_charge_for_pubdata` calls `net_pubdata_used` again, iterating all 300,000 entries a second time.
6. The native cycle cost of these two O(N) iterations is not charged to the transaction's gas, creating an unmetered native cost that can exhaust the block's `MAX_NATIVE_COMPUTATIONAL` budget or cause the prover to fail.

**Root cause files:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3) 
- [5](#0-4) 
- [6](#0-5) 
- [7](#0-6)

### Citations

**File:** basic_system/src/system_implementation/flat_storage_model/storage_cache.rs (L265-308)
```rust
    pub fn calculate_pubdata_used_by_tx(&self) -> u32 {
        let mut visited_elements = BTreeSet::new_in(self.0.alloc.clone());

        let mut pubdata_used = 0u32;
        for element_history in self.0.cache.iter_altered_since_commit() {
            // Elements are sorted chronologically

            let element_key = element_history.key();

            // we publish preimages for account details, so no need to publish hash
            if element_key.address == ACCOUNT_PROPERTIES_STORAGE_ADDRESS {
                continue;
            }

            // Skip if already calculated pubdata for this element
            if visited_elements.contains(element_key) {
                continue;
            }
            visited_elements.insert(element_key);

            let current_value = element_history.current().value();
            let initial_value = element_history.initial().value();
            let at_tx_start_value = element_history.committed().value();

            // If the current value is resetting to the initial one,
            // we don't consider this diff in the pubdata charging.
            // This change will be optimized away, so it's actually reducing
            // pubdata.
            if current_value == initial_value {
                continue;
            }

            if at_tx_start_value != current_value {
                // TODO(EVM-1074): use tree index instead of key for repeated writes
                pubdata_used += 32; // key
                pubdata_used += ValueDiffCompressionStrategy::optimal_compression_length(
                    at_tx_start_value,
                    current_value,
                ) as u32;
            }
        }

        pubdata_used
    }
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L433-473)
```rust
    pub fn calculate_pubdata_used_by_tx(&self) -> u32 {
        let mut visited_elements = BTreeSet::new_in(self.alloc.clone());

        let mut pubdata_used = 0u32;
        for element_history in self.cache.iter_altered_since_commit() {
            // Elements are sorted chronologically

            let element_key = element_history.key();

            // Skip if already calculated pubdata for this element
            if visited_elements.contains(element_key) {
                continue;
            }
            visited_elements.insert(element_key);

            let current = element_history.current();
            let initial = element_history.initial();
            let at_tx_start = element_history.committed();

            // If the current value is resetting to the initial one,
            // we don't consider this diff in the pubdata charging.
            // This change will be optimized away, so it's actually reducing
            // pubdata.
            if current.value() == initial.value() && !current.metadata().not_compress_balance {
                continue;
            }

            if current.value() != at_tx_start.value() || current.metadata().not_compress_balance {
                pubdata_used += 32; // key
                pubdata_used += AccountProperties::diff_compression_length(
                    at_tx_start.value(),
                    current.value(),
                    current.metadata().not_publish_bytecode,
                    current.metadata().not_compress_balance,
                )
                .unwrap();
            }
        }

        pubdata_used
    }
```

**File:** zk_ee/src/common_structs/history_map/mod.rs (L187-204)
```rust
    /// Commits (freezes) changes up to this point and frees memory taken by snapshots that can't be
    /// rolled back to.
    pub fn commit(&mut self) {
        self.state.frozen_snapshot_id = self.snapshot();

        // Go over all elements changed since last `commit` and `commit` their history
        for (key, _) in self.state.pending_updated_elements.iter() {
            let item = self
                .btree
                .get_mut(key)
                .expect("We've updated this, so it must be present.");

            item.commit(&mut self.records_memory_pool);
        }

        // We've committed, so we don't need those changes anymore.
        self.state.pending_updated_elements = StackLinkedList::empty(self.state.alloc.clone());
    }
```

**File:** zk_ee/src/common_structs/history_map/mod.rs (L251-264)
```rust
    pub fn iter_altered_since_commit(
        &'_ self,
    ) -> impl Iterator<Item = HistoryMapItemRef<'_, K, V, A, KP>> {
        self.state
            .pending_updated_elements
            .iter()
            .map(|(k, _)| HistoryMapItemRef {
                key: k,
                history: self
                    .btree
                    .get(k)
                    .expect("We've updated this, so it must be present."),
            })
    }
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L397-400)
```rust
    fn net_pubdata_used(&self) -> Result<u64, InternalError> {
        Ok(self.storage.pubdata_used_by_tx() as u64
            + self.logs_storage.calculate_pubdata_used_by_tx()? as u64)
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L375-425)
```rust
    fn before_refund<'a, Config: BasicBootloaderExecutionConfig>(
        system: &mut System<S>,
        transaction: &Transaction<<S as SystemTypes>::Allocator>,
        context: &mut Self::TransactionContext,
        _result: &ExecutionResult<'a, <S as SystemTypes>::IOTypes>,
        pubdata_info: Self::ExecutionBodyExtraData,
        _tracer: &mut impl Tracer<S>,
    ) -> Result<(), BootloaderSubsystemError> {
        use evm_interpreter::ERGS_PER_GAS;

        // Just used for computing native used
        context.resources_before_refund = context.resources.main_resources.clone();

        // Now we can actually reclaim resources withheld for pubdata
        context
            .resources
            .main_resources
            .reclaim_withheld(context.resources.withheld.take());

        system_log!(
            system,
            "Have {:?} resources available before refund, and need to cover {:?} pubdata\n",
            &context.resources.main_resources,
            &pubdata_info
        );

        let intrinsic_pubdata = calculate_l2_tx_intrinsic_pubdata(
            context.authorization_list_num,
            transaction.is_service(),
        );

        // Pubdata for validation has been charged already,
        // we charge for the rest now.
        let (total_pubdata_used, to_charge_for_pubdata) = match pubdata_info {
            Some(CachedPubdataInfo {
                pubdata_used,
                to_charge_for_pubdata,
            }) => (pubdata_used + intrinsic_pubdata, to_charge_for_pubdata),
            None => {
                let (execution_pubdata_spent, to_charge_for_pubdata) =
                    get_resources_to_charge_for_pubdata(
                        system,
                        context.native_per_pubdata,
                        Some(context.validation_pubdata),
                    )?;
                (
                    execution_pubdata_spent + intrinsic_pubdata,
                    to_charge_for_pubdata,
                )
            }
        };
```
