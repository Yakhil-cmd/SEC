### Title
Unbounded Uncharged Loop in `clear_state_impl` During SELFDESTRUCT Post-Processing — (`basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs`)

---

### Summary

When a contract deployed in the same transaction calls `SELFDESTRUCT`, the post-execution `finish_tx` hook calls `clear_state_impl`, which iterates over **all cached storage slots** for the deconstructed address and zeroes them out. This loop is proportional to the number of storage slots touched during the transaction, but **no native resources are charged** for any iteration. An attacker can write to an arbitrarily large number of storage slots before selfdestructing, causing the system to perform O(N) uncharged proving work.

---

### Finding Description

`clear_state_impl` in `GenericPubdataAwarePlainStorage` iterates over every cached storage slot in the address range and updates each one to its default value:

```rust
// basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs:296-315
pub fn clear_state_impl(&mut self, address: impl AsRef<B160>) -> Result<(), SystemError>
where
    K::Subspace: TyEq<B160>,
{
    use core::ops::Bound::Included;
    let lower_bound = K::lower_bound(TyEq::rwi(*address.as_ref()));
    let upper_bound = K::upper_bound(TyEq::rwi(*address.as_ref()));
    self.cache
        .for_each_range((Included(&lower_bound), Included(&upper_bound)), |mut x| {
            x.update(|cache_record| {
                cache_record.update(|v, _| {
                    *v = V::default();
                    Ok(())
                })
            })
        })?;
    Ok(())
}
``` [1](#0-0) 

Each `x.update()` call allocates a new history record in the `HistoryMap`, which is real computational work that must be proven. The function signature takes no `resources` parameter, so **zero native resources are charged** for any number of iterations.

This function is called unconditionally from `finish_tx` in both storage model implementations whenever an account is marked for deconstruction:

**Flat storage model:**
```rust
// basic_system/src/system_implementation/flat_storage_model/account_cache.rs:1248-1251
storage
    .0
    .clear_state_impl(key)
    .expect("must clear state for code deconstruction in same TX");
``` [2](#0-1) 

**Ethereum storage model:**
```rust
// basic_system/src/system_implementation/ethereum_storage_model/caches/account_cache.rs:825-828
storage
    .slot_values
    .clear_state_impl(key)
    .expect("must clear state for code deconstruction in same TX");
``` [3](#0-2) 

`finish_tx` itself takes no `resources` parameter: [4](#0-3) 

The `SnapshottableIo::finish_tx` that calls it also takes no resources: [5](#0-4) 

The `for_each_range` underlying the loop iterates over the BTreeMap range with no bound: [6](#0-5) 

---

### Impact Explanation

ZKsync OS uses a **dual resource model**: EVM gas (ergs) and native resources (proving cycles). Native resources are charged per operation to ensure the prover is compensated for its work. The `clear_state_impl` loop performs O(N) `HistoryMap::update` operations — each equivalent in proving cost to a warm storage write (`WARM_STORAGE_WRITE_EXTRA_NATIVE_COST = 1000` native units) — but charges **zero** native resources. [7](#0-6) 

An attacker who writes to N storage slots and then selfdestructs causes the system to perform N additional uncharged `HistoryMap` update operations in `clear_state_impl`. This is a **proving cost amplification**: the attacker pays for N cold writes (each ~100,000 native) but forces an additional N warm-write-equivalent operations (~1,000 native each) without compensation. While the ratio is ~1%, at scale (e.g., N = 1,500 slots within a 30M gas transaction) this represents ~1,500,000 uncharged native units per transaction — a systematic undercharging of proving cost that can be exploited repeatedly to grief the sequencer/prover.

---

### Likelihood Explanation

**Low.** The attack requires:
1. Deploying a contract in the same transaction (EIP-6780 restriction enforced at line 1161–1163 of `account_cache.rs`).
2. Writing to many storage slots within that contract before selfdestructing.
3. The attacker must pay EVM gas for each SSTORE, limiting the amplification ratio. [8](#0-7) 

The constraint that deconstruction only applies to same-transaction deployments limits the attack surface, but the pattern is straightforwardly reachable by any unprivileged EVM transaction sender.

---

### Recommendation

Charge native resources inside `clear_state_impl` proportional to the number of slots cleared. One approach: pass a `resources: &mut R` parameter through `finish_tx` → `clear_state_impl` and charge `WARM_STORAGE_WRITE_EXTRA_NATIVE_COST` per slot cleared. Alternatively, track the number of slots written during the transaction and pre-charge for the expected clearing cost at SELFDESTRUCT time (when resources are still available).

---

### Proof of Concept

1. Attacker sends a transaction that:
   - Deploys a contract via `CREATE` (marks it with `deployed_in_tx = Some(cur_tx)`).
   - Within the constructor or a subsequent same-tx call, executes N `SSTORE` operations to N distinct slots (paying cold-write gas for each).
   - Calls `SELFDESTRUCT` on the deployed contract.
2. Transaction execution completes; `mark_for_deconstruction` sets `is_marked_for_deconstruction = true`.
3. `SnapshottableIo::finish_tx` is called post-execution with no resource parameter.
4. `account_data_cache.finish_tx(&mut self.storage_cache)` iterates over pending changes, finds the deconstructed account, and calls `storage.0.clear_state_impl(key)`.
5. `clear_state_impl` calls `for_each_range` over all N cached slots, executing N `HistoryMap::update` calls — each creating a new history record — with zero native resource charge.
6. The prover must prove N additional operations that were not accounted for in the transaction's native resource budget. [1](#0-0) [9](#0-8) [10](#0-9)

### Citations

**File:** basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs (L296-315)
```rust
    /// Clear state at specified address
    pub fn clear_state_impl(&mut self, address: impl AsRef<B160>) -> Result<(), SystemError>
    where
        K::Subspace: TyEq<B160>,
    {
        use core::ops::Bound::Included;
        let lower_bound = K::lower_bound(TyEq::rwi(*address.as_ref()));
        let upper_bound = K::upper_bound(TyEq::rwi(*address.as_ref()));
        self.cache
            .for_each_range((Included(&lower_bound), Included(&upper_bound)), |mut x| {
                x.update(|cache_record| {
                    cache_record.update(|v, _| {
                        *v = V::default();
                        Ok(())
                    })
                })
            })?;

        Ok(())
    }
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L1160-1176)
```rust
        let in_constructor = account_data.current().value().observable_bytecode_len == 0;
        let should_be_deconstructed = account_data.current().metadata().basic.deployed_in_tx
            == Some(cur_tx)
            || in_constructor;

        if should_be_deconstructed {
            account_data
                .element_properties_mut()
                .mark_value_as_observed();
            account_data.update(|data| {
                data.update_metadata(|metadata| {
                    metadata.basic.is_marked_for_deconstruction = true;

                    Ok(())
                })
            })?;
        }
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L1230-1258)
```rust
    pub fn finish_tx(
        &mut self,
        storage: &mut NewStorageWithAccountPropertiesUnderHash<A, SF, M, R, P>,
    ) -> Result<(), InternalError> {
        self.current_tx_id += 1;

        // Actually deconstructing accounts
        self.cache.apply_to_last_record_of_pending_changes(
            |key, (_initial, current), cache_appearance| {
                if current.value.metadata().basic.is_marked_for_deconstruction {
                    // NOTE: it can only happen if the account is initially empty,
                    // so we need to make sure that it was observed earlier - when bytecode was deployed
                    assert!(cache_appearance.is_value_observed());
                    current.value.update(|x, metadata| {
                        metadata.basic.is_marked_for_deconstruction = false;
                        *x = AccountProperties::TRIVIAL_VALUE;
                        Ok(())
                    })?;
                    storage
                        .0
                        .clear_state_impl(key)
                        .expect("must clear state for code deconstruction in same TX");
                }
                Ok(())
            },
        )?;

        Ok(())
    }
```

**File:** basic_system/src/system_implementation/ethereum_storage_model/caches/account_cache.rs (L786-835)
```rust
    pub fn finish_tx<P: StorageAccessPolicy<R, Bytes32>>(
        &mut self,
        storage: &mut EthereumStorageCache<A, SF, N, R, P>,
    ) -> Result<(), InternalError> {
        // Actually deconstructing accounts
        self.cache.apply_to_last_record_of_pending_changes(
            |key, (initial, current), cache_appearance| {
                if current.value.metadata().is_marked_for_deconstruction {
                    // NOTE: initially account had 0 nonce, but it could be "material",
                    // with state root being empty, and bytecode hash being hash of empty string.

                    // NOTE: Balance will be zeroed out if deconstruction happens here
                    let initially_empty = cache_appearance.is_new_element();
                    assert!(cache_appearance.is_value_observed());
                    current.value.update(|x, metadata| {
                        metadata.is_marked_for_deconstruction = false;
                        if initially_empty {
                            debug_assert_eq!(
                                initial.value.value(),
                                &EthereumAccountProperties::EMPTY_ACCOUNT
                            );
                            x.balance = U256::ZERO;
                            x.bytecode_hash = Bytes32::ZERO;
                            x.nonce = 0u64;
                        } else {
                            //
                            debug_assert_eq!(initial.value.value().nonce, 0);
                            debug_assert_eq!(
                                initial.value.value().bytecode_hash,
                                EMPTY_STRING_KECCAK_HASH
                            );
                            debug_assert_eq!(initial.value.value().storage_root, EMPTY_ROOT_HASH);
                            x.balance = U256::ZERO;
                            x.bytecode_hash = EMPTY_STRING_KECCAK_HASH;
                            x.nonce = 0u64;
                        }

                        Ok(())
                    })?;
                    storage
                        .slot_values
                        .clear_state_impl(key)
                        .expect("must clear state for code deconstruction in same TX");
                }
                Ok(())
            },
        )?;

        Ok(())
    }
```

**File:** basic_system/src/system_implementation/flat_storage_model/mod.rs (L519-523)
```rust
    fn finish_tx(&mut self) -> Result<(), zk_ee::system::errors::internal::InternalError> {
        self.account_data_cache.finish_tx(&mut self.storage_cache)?;
        self.storage_cache.finish_tx()?;
        self.preimages_cache.finish_tx()
    }
```

**File:** zk_ee/src/common_structs/history_map/mod.rs (L221-239)
```rust
    pub fn for_each_range<F>(
        &mut self,
        range: (Bound<&K>, Bound<&K>),
        mut do_fn: F,
    ) -> Result<(), InternalError>
    where
        F: FnMut(HistoryMapItemRefMut<K, V, A, KP>) -> Result<(), InternalError>,
    {
        for (k, v) in self.btree.range_mut(range) {
            do_fn(HistoryMapItemRefMut {
                key: &k,
                history: v,
                cache_state: &mut self.state,
                records_memory_pool: &mut self.records_memory_pool,
            })?
        }

        Ok(())
    }
```

**File:** basic_system/src/system_implementation/flat_storage_model/cost_constants.rs (L13-22)
```rust
pub const WARM_STORAGE_READ_NATIVE_COST: u64 = 4000;
// Avg is ~10x smaller, maybe we can reduce it, but it depends on cache state.
pub const WARM_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 = 1000;
// Estimation based on worst-case
pub const COLD_EXISTING_STORAGE_READ_NATIVE_COST: u64 = native_with_delegations!(100_000, 0, 1320);
pub const COLD_NEW_STORAGE_READ_NATIVE_COST: u64 = 2 * COLD_EXISTING_STORAGE_READ_NATIVE_COST;
pub const COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(40_000, 0, 660);
pub const COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(100_000, 0, 1300);
```

**File:** evm_interpreter/src/instructions/host.rs (L254-285)
```rust
    pub fn selfdestruct(
        &mut self,
        system: &mut System<S>,
        tracer: &mut impl Tracer<S>,
    ) -> InstructionResult {
        self.gas
            .spend_gas_and_native(gas_constants::SELFDESTRUCT, SELFDESTRUCT_NATIVE_COST)?;

        if self.is_static_frame() {
            return Err(EvmError::StateChangeDuringStaticCall.into());
        }

        let beneficiary = u256_to_b160(self.stack.pop_1()?);

        let amount_transferred = system
            .io
            .mark_for_deconstruction(
                THIS_EE_TYPE,
                self.gas.resources_mut(),
                &self.address,
                &beneficiary,
            )
            .map_err(wrap_error!())?;

        tracer.evm_tracer().on_selfdestruct(
            beneficiary,
            amount_transferred,
            &InterpreterExternal::new_from(&self, system),
        );

        Err(ExitCode::SelfDestruct)
    }
```
