### Title
Unbounded Iteration in `calculate_pubdata_used_by_tx` Without Native Resource Charging — (File: `basic_system/src/system_implementation/flat_storage_model/storage_cache.rs`)

---

### Summary

`calculate_pubdata_used_by_tx` iterates over all storage history entries accumulated since the last transaction commit (`iter_altered_since_commit()`), but charges **zero native resources** for the iteration work. This function is invoked multiple times per transaction via `net_pubdata_used()` → `pubdata_used_by_tx()`. An attacker can craft a transaction that performs many warm SSTORE operations (each costing only 100 gas) to inflate the history-entry count, causing the prover to execute significantly more RISC-V instructions than the native resource budget accounts for, leading to valid-execution unprovability.

---

### Finding Description

`calculate_pubdata_used_by_tx` in the flat storage model iterates over every history entry recorded since the last `commit()` (i.e., since `begin_new_tx()`):

```rust
for element_history in self.0.cache.iter_altered_since_commit() {
    if visited_elements.contains(element_key) { continue; }
    visited_elements.insert(element_key);
    ...
}
```

The deduplication via `visited_elements` (a `BTreeSet`) confirms that `iter_altered_since_commit()` can yield the **same key multiple times** — once per write operation, not once per unique slot. A slot written `K` times produces `K` history entries, all iterated before deduplication. The BTreeSet insert/lookup is itself O(log N) per entry.

This function is called (without any native resource charge for the iteration) through the following call chain, invoked **multiple times per transaction**:

1. `before_execute_transaction_payload` → `system.net_pubdata_used()` (line 297 in `zk/mod.rs`)
2. `execute_or_deploy_inner` → `check_enough_resources_for_pubdata` → `system.net_pubdata_used()` (line 880 in `zk/mod.rs`)
3. `before_refund` (on revert path) → `get_resources_to_charge_for_pubdata` → `system.net_pubdata_used()` (line 415 in `zk/mod.rs`)

`net_pubdata_used()` delegates to `pubdata_used_by_tx()` which calls both `storage_cache.calculate_pubdata_used_by_tx()` and `account_data_cache.calculate_pubdata_used_by_tx()`, neither of which charges native resources for their iteration.

The analogous function in the account cache has the same pattern:

```rust
for element_history in self.cache.iter_altered_since_commit() {
    if visited_elements.contains(element_key) { continue; }
    visited_elements.insert(element_key);
    ...
}
```

---

### Impact Explanation

ZKsync OS uses a dual-resource model: EVM gas (ergs) and **native resources** representing the RISC-V proving cost. The prover runs the same Rust code as the sequencer. If `calculate_pubdata_used_by_tx` iterates over `K` history entries, the prover executes O(K log K) RISC-V instructions for the BTreeSet operations — but the native resource counter is never decremented for this work.

An attacker who maximizes `K` (by writing to the same slot repeatedly) can cause the prover to exhaust its native resource budget mid-execution even though the sequencer's forward execution succeeded. The result is **valid-execution unprovability**: the sequencer accepts the block, but the prover cannot generate a valid proof, halting the chain.

---

### Likelihood Explanation

A warm SSTORE costs 100 gas. With a block gas limit of 30 M gas, a single transaction can perform up to ~300 K warm SSTOREs (after the initial cold write). Each SSTORE appends a history entry. `calculate_pubdata_used_by_tx` is called at least twice per transaction (once pre-execution, once post-execution), so the prover must iterate over ~600 K entries total — all uncharged. This is reachable by any unprivileged sender deploying a simple loop contract. No privileged access, oracle manipulation, or external dependency is required.

---

### Recommendation

Charge native resources proportional to the number of history entries iterated inside `calculate_pubdata_used_by_tx`. One approach is to track the history-entry count as a cached counter incremented on each write and reset on `begin_new_tx()`, so `pubdata_used_by_tx()` can be O(1) and the per-write native cost already covers the amortized iteration cost. Alternatively, add an explicit native charge inside the loop body proportional to the number of entries processed.

---

### Proof of Concept

**Attacker contract (Solidity pseudocode):**
```solidity
contract Spammer {
    uint256 slot0;
    fallback() external {
        for (uint256 i = 0; i < 200_000; i++) {
            slot0 = i;   // warm SSTORE: 100 gas each
        }
    }
}
```

**Attack transaction:** Call `Spammer` with `gas_limit = 30_000_000`.

**Effect:**
- 200 K warm SSTOREs → 200 K history entries in `iter_altered_since_commit()`
- `calculate_pubdata_used_by_tx` is called ≥2× per transaction → ≥400 K BTreeSet operations (O(log N) each) executed by the prover with zero native resource charge
- Sequencer forward execution succeeds (native budget not exhausted because iteration is uncharged)
- Prover executes the same uncharged loop, exhausting its native resource budget → block is unprovable

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** basic_system/src/system_implementation/flat_storage_model/mod.rs (L118-121)
```rust
    fn pubdata_used_by_tx(&self) -> u32 {
        self.account_data_cache.calculate_pubdata_used_by_tx()
            + self.storage_cache.calculate_pubdata_used_by_tx()
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L880-906)
```rust
        let (has_enough, to_charge_for_pubdata, pubdata_used) = check_enough_resources_for_pubdata(
            system,
            context.native_per_pubdata,
            &resources_for_check,
            Some(context.validation_pubdata),
        )?;
        if !has_enough {
            execution_result = execution_result.to_reverted();
            system_log!(system, "Not enough gas for pubdata after execution\n");
            // Burn all remaining ergs.
            context.resources.main_resources.exhaust_ergs();
            Ok((
                execution_result.to_reverted(),
                CachedPubdataInfo {
                    pubdata_used,
                    to_charge_for_pubdata,
                },
            ))
        } else {
            Ok((
                execution_result,
                CachedPubdataInfo {
                    pubdata_used,
                    to_charge_for_pubdata,
                },
            ))
        }
```
