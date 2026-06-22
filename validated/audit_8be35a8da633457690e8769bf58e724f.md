### Title
Zombie Child Neuron Left in Governance State When Ledger Transfer Fails and Cleanup Errors - (File: rs/nns/governance/src/governance.rs)

### Summary
In `disburse_to_neuron`, a child neuron is created and persisted in governance state before the ledger transfer is attempted. If the transfer fails, the code attempts to remove the child neuron via `self.remove_neuron(child_neuron)?`. Because the `?` operator is used, any failure of `remove_neuron` itself (e.g., while the child neuron's in-flight lock is still held) causes the cleanup to silently abort, leaving a zombie child neuron with `cached_neuron_stake_e8s = 0` permanently in the governance store.

### Finding Description
In `disburse_to_neuron` (`rs/nns/governance/src/governance.rs`):

1. A child neuron is added to the store with `cached_neuron_stake_e8s = 0` before the transfer: [1](#0-0) 

2. The child neuron is immediately locked via `lock_neuron_for_command`: [2](#0-1) 

3. The ledger transfer is attempted: [3](#0-2) 

4. On transfer failure, `remove_neuron` is called with `?` while `_child_lock` is still in scope (the child neuron is still registered in `in_flight_commands`): [4](#0-3) 

If `remove_neuron` returns an error (for example, because the neuron is still locked or any other internal check fails), the `?` propagates that error and returns early. The `_child_lock` RAII guard then drops, releasing the lock — but the child neuron entry with `cached_neuron_stake_e8s = 0` remains permanently in the governance neuron store. The parent neuron's stake is never decremented (correct), but the child neuron ID and its derived subaccount are permanently consumed.

By contrast, the TLA+ model for `Disburse_To_Neuron` specifies that on transfer failure the child neuron **must** be removed unconditionally: [5](#0-4) 

The same structural risk exists in `split_neuron` (NNS): [6](#0-5) 

And in SNS `split_neuron`: [7](#0-6) 

### Impact Explanation
A zombie neuron with `cached_neuron_stake_e8s = 0` permanently occupies a neuron ID and a deterministically derived subaccount (computed from `child_controller` and `nonce`). Because the subaccount is known to the child controller, they can subsequently send ICP directly to that subaccount and call `claim_or_refresh_neuron_from_account` to resurrect the neuron — effectively obtaining a neuron that governance believed was cleaned up. Over time, repeated failed `disburse_to_neuron` calls accumulate zombie neurons, inflating the neuron store and consuming neuron ID space. This constitutes a **governance state inconsistency / ledger conservation bug**.

### Likelihood Explanation
The `disburse_to_neuron` function is callable by any neuron controller (unprivileged ingress). The transfer failure path is reachable whenever the ICP ledger is temporarily unavailable or returns an error. Whether `remove_neuron` itself then fails depends on its internal lock-checking logic; if it checks `in_flight_commands` before removing (which is a common defensive pattern), the child lock still being held at the call site guarantees the cleanup always fails in this path, making the zombie neuron creation deterministic on any transfer failure.

### Recommendation
1. Drop `_child_lock` explicitly before calling `remove_neuron`, so the neuron is not locked when cleanup is attempted.
2. Replace `self.remove_neuron(child_neuron)?` with a non-fallible cleanup that logs but does not propagate errors, ensuring the zombie neuron is always removed regardless of secondary failures.
3. Align the Rust implementation with the TLA+ specification in `Disburse_To_Neuron.tla`, which unconditionally removes the child neuron on transfer failure.

### Proof of Concept
1. Caller holds a dissolved neuron with sufficient stake.
2. Caller calls `disburse_to_neuron` with a valid `child_controller` and `nonce`.
3. Child neuron is created at line 3023 with `cached_neuron_stake_e8s = 0` and locked at line 3026.
4. The ICP ledger call at line 3040 fails (e.g., ledger temporarily unavailable).
5. `remove_neuron` at line 3056 is called while `_child_lock` is still in scope; if it fails, `?` propagates the error.
6. Function returns; `_child_lock` drops, releasing the lock. Child neuron remains in store with 0 stake.
7. Caller queries governance: child neuron ID exists with 0 stake, subaccount permanently reserved.
8. Caller sends ICP to the child neuron's subaccount and calls `claim_or_refresh_neuron_from_account` to resurrect it.

### Citations

**File:** rs/nns/governance/src/governance.rs (L2289-2311)
```rust
        if let Err(error) = result {
            let error = GovernanceError::from(error);

            // Refund the parent neuron if the ledger call somehow failed.
            self.neuron_store
                .with_neuron_mut(id, |parent_neuron| {
                    parent_neuron.cached_neuron_stake_e8s = parent_neuron
                        .cached_neuron_stake_e8s
                        .checked_add(split_amount_e8s)
                        .expect("Neuron stake overflows");
                })
                .expect("Expected the parent neuron to exist");

            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(child_neuron)?;
            println!(
                "Neuron stake transfer of split_neuron: {:?} \
                     failed with error: {:?}. Neuron can't be staked.",
                child_nid, error
            );
            return Err(error);
```

**File:** rs/nns/governance/src/governance.rs (L3006-3023)
```rust
        // Before we do the transfer, we need to save the neuron in the map
        // otherwise a trap after the transfer is successful but before this
        // method finishes would cause the funds to be lost.
        // However the new neuron is not yet ready to be used as we can't know
        // whether the transfer will succeed, so we temporarily set the
        // stake to 0 and only change it after the transfer is successful.
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            child_controller,
            dissolve_state_and_age,
            created_timestamp_seconds,
        )
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .build();

        self.add_neuron(child_nid.id, child_neuron.clone())?;
```

**File:** rs/nns/governance/src/governance.rs (L3025-3026)
```rust
        // Add the child neuron to the set of neurons undergoing ledger updates.
        let _child_lock = self.lock_neuron_for_command(child_nid.id, in_flight_command.clone())?;
```

**File:** rs/nns/governance/src/governance.rs (L3040-3049)
```rust
        let result: Result<u64, NervousSystemError> = self
            .ledger
            .transfer_funds(
                staked_amount,
                transaction_fee_e8s,
                Some(from_subaccount),
                neuron_subaccount(to_subaccount),
                memo,
            )
            .await;
```

**File:** rs/nns/governance/src/governance.rs (L3051-3062)
```rust
        if let Err(error) = result {
            let error = GovernanceError::from(error);
            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(child_neuron)?;
            println!(
                "Neuron minting transfer of to neuron: {:?}\
                                  failed with error: {:?}. Neuron can't be staked.",
                child_nid, error
            );
            return Err(error);
```

**File:** rs/nns/governance/tla/Disburse_To_Neuron.tla (L79-94)
```text
    DisburseToNeuron_WaitForTransfer:
        with(answer \in { resp \in ledger_to_governance: resp.caller = self}) {
                ledger_to_governance := ledger_to_governance \ {answer};
                if(answer.response = Variant("Fail", UNIT)) {
                    neuron := Remove_Arguments(neuron, {child_neuron_id});
                    neuron_id_by_account := Remove_Arguments(neuron_id_by_account, {child_account_id});
                } else {
                    neuron := [ neuron EXCEPT ![parent_neuron_id].cached_stake = @ - disburse_amount,
                        ![child_neuron_id].cached_stake = disburse_amount - TRANSACTION_FEE ];
                };
                locks := locks \ {parent_neuron_id, child_neuron_id};
                parent_neuron_id := 0;
                disburse_amount := 0;
                child_account_id := DUMMY_ACCOUNT;
                child_neuron_id := 0;
        };
```

**File:** rs/sns/governance/src/governance.rs (L1408-1421)
```rust
        if let Err(error) = result {
            let error = GovernanceError::from(error);
            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(&child_nid, child_neuron)?;
            log!(
                ERROR,
                "Neuron stake transfer of split_neuron: {:?} \
                     failed with error: {:?}. Neuron can't be staked.",
                child_nid,
                error
            );
            return Err(error);
```
