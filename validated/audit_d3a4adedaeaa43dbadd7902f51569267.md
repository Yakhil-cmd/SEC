### Title
EVM Refund Counter Not Reset Between Transactions Allows Cross-Transaction Gas Refund Leakage — (`basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs`)

---

### Summary

The `evm_refunds_counter` in `GenericPubdataAwarePlainStorage` accumulates EVM gas refunds (from SSTORE operations that zero storage slots) across transactions within a block. Because the counter is never reset between transactions, refunds earned by one transaction carry over and inflate the gas refund calculation of subsequent transactions, allowing a sender to receive undeserved gas refunds at the operator's expense.

---

### Finding Description

`GenericPubdataAwarePlainStorage` holds a persistent `evm_refunds_counter` field of type `NonEmptyHistoryCounter<R, SF, M, A>`: [1](#0-0) 

The counter is incremented by `add_to_refund_counter_impl` whenever an EVM SSTORE operation zeroes a storage slot: [2](#0-1) 

The counter is read in `compute_gas_refund` to reduce `gas_used` for the **current** transaction: [3](#0-2) 

The snapshot mechanism (`StorageSnapshotId`) includes the counter, so it is correctly **rolled back** when a transaction reverts. However, when a transaction **commits** via `finish_global_frame(None)`, the counter value is preserved and carries over to the next transaction. There is no code path that resets the counter to zero at the start of a new transaction. The `current_tx_id` field is incremented per transaction (for warm/cold slot tracking), but the `evm_refunds_counter` has no analogous reset. [4](#0-3) 

This is the direct analog of the external report: a state variable tracking a "share" (here, the EVM gas refund pool) is not properly scoped to the current operation, so a later operation incorrectly inherits the accumulated value.

---

### Impact Explanation

A transaction that commits large SSTORE-zeroing refunds (e.g., 480,000 gas worth from 100 slot clears) leaves the counter non-zero. The immediately following transaction reads this stale counter value and, subject to the EIP-3529 cap of `gas_used / 5`, receives a gas refund it did not earn. The operator (coinbase) is paid less than the correct fee; the sender is refunded more than they are owed. This is a direct funds-loss path for the operator and an unearned gain for the attacker.

---

### Likelihood Explanation

The attacker submits two transactions in sequence: Transaction A zeros many storage slots (generating large refunds), and Transaction B is the attacker's own transaction that benefits from the leaked counter. In ZKsync OS the sequencer controls ordering, but the attacker can influence ordering by setting gas prices appropriately, or can be the sequencer itself. The attack requires no privileged role beyond normal transaction submission.

---

### Recommendation

Reset `evm_refunds_counter` to zero at the start of each new transaction, analogous to how `current_tx_id` is incremented. This can be done in the `new_tx` or equivalent initialization path of `GenericPubdataAwarePlainStorage`, or by resetting the counter immediately after `compute_gas_refund` consumes it.

---

### Proof of Concept

1. **Transaction A** (attacker): calls a contract that clears 100 storage slots via SSTORE. Each clear adds `4800 * ERGS_PER_GAS` to `evm_refunds_counter`. After commit, counter = 480,000 gas equivalent.
2. **Transaction B** (attacker): a simple ETH transfer using 1,000,000 gas, generating zero SSTORE refunds of its own.
3. `compute_gas_refund` for Transaction B reads `get_refund_counter()` = 480,000 gas. The EIP-3529 cap is `1,000,000 / 5 = 200,000` gas. `evm_refund = min(480,000, 200,000) = 200,000` gas.
4. Transaction B's `gas_used` is reduced by 200,000 gas. The operator receives `200,000 * gas_price` fewer tokens than owed. The attacker's sender address is refunded `200,000 * gas_price` tokens it did not earn. [5](#0-4) [6](#0-5)

### Citations

**File:** basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs (L49-72)
```rust
#[derive(Debug)]
pub struct StorageSnapshotId {
    pub cache: CacheSnapshotId,
    pub evm_refunds_counter: HistoryCounterSnapshotId,
}

pub struct GenericPubdataAwarePlainStorage<
    K: KeyLikeWithBounds,
    V,
    A: Allocator + Clone, // = Global,
    SF: StackFactory<M>,
    const M: usize,
    R: Resources,
    P: StorageAccessPolicy<R, V>,
> {
    pub(crate) cache:
        HistoryMap<K, CacheRecord<V, StorageElementMetadata>, A, CacheElementProperties>,
    pub(crate) resources_policy: P,
    // Note: this doesn't need to be equal to the actual tx number in the block, it just needs to be able to differentiate between transactions.
    pub(crate) current_tx_id: TransactionId,
    pub(crate) evm_refunds_counter: NonEmptyHistoryCounter<R, SF, M, A>, // Used to keep track of EVM gas refunds
    pub(crate) alloc: A,
    pub(crate) _marker: core::marker::PhantomData<(R, SF)>,
}
```

**File:** basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs (L317-319)
```rust
    pub fn get_refund_counter_impl(&'_ self) -> &'_ R {
        self.evm_refunds_counter.value()
    }
```

**File:** basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs (L321-326)
```rust
    pub fn add_to_refund_counter_impl(&mut self, refund: R) -> Result<(), SystemError> {
        let mut t = self.get_refund_counter_impl().clone();
        t.add_ergs(refund.ergs());
        self.evm_refunds_counter.update(t);
        Ok(())
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L31-48)
```rust
    let mut gas_used = gas_limit
        .checked_sub(resources.ergs().0.div_floor(ERGS_PER_GAS))
        .ok_or(internal_error!("gas remaining > gas limit"))?;
    resources.exhaust_ergs();

    system_log!(system, "Gas used before refund calculations: {gas_used}\n");

    // Following EIP-3529, refunds are capped to 1/5 of the gas used
    let evm_refund = {
        let full_refund_ergs = system.io.get_refund_counter().ergs();
        let full_refund_gas = full_refund_ergs.0.div_floor(ERGS_PER_GAS);
        let max_refund = gas_used / 5;
        core::cmp::min(full_refund_gas, max_refund)
    };

    system_log!(system, "Gas refund from refund counters = {evm_refund}\n");

    gas_used -= evm_refund;
```
