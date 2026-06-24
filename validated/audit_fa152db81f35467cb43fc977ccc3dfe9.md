Audit Report

## Title
SNS Governance Neuron Population Limit Counts Fully-Disbursed Zero-Stake Neurons, Enabling Permanent DoS on New Neuron Creation - (File: rs/sns/governance/src/governance.rs)

## Summary

`check_neuron_population_can_grow` enforces the neuron ceiling by counting `self.proto.neurons.len()`, which includes every neuron ever created regardless of stake. `disburse_neuron` zeroes `cached_neuron_stake_e8s` in-place but never calls `remove_neuron`, so each create-then-disburse cycle permanently increments the count by one. Once `max_number_of_neurons` is reached, no new neurons can ever be created in the SNS, permanently blocking all new governance participation.

## Finding Description

`check_neuron_population_can_grow` at line 6371 compares `(self.proto.neurons.len() as u64) + 1 > max_number_of_neurons` with no distinction between live and zero-stake neurons. [1](#0-0) 

`add_neuron` calls `check_neuron_population_can_grow` at line 951 before every neuron insertion, making it the sole gate for all neuron creation paths (`claim_or_refresh`, `split_neuron`, `claim_swap_neurons`). [2](#0-1) 

`disburse_neuron` completes the ledger transfer and then only decrements `cached_neuron_stake_e8s` at line 1234; it does not call `remove_neuron` and does not delete the entry from `proto.neurons`. [3](#0-2) 

`remove_neuron` exists at lines 983–1006 and correctly removes the entry from `proto.neurons` plus all index entries, but it is never invoked from the disburse path. [4](#0-3) 

The proto documentation for `max_number_of_neurons` states "When this maximum is reached, no new neurons will be created until some are removed," but there is no user-facing command to remove a neuron and no automatic garbage-collection path for zero-stake neurons in SNS Governance, making the escape hatch unreachable in practice.

## Impact Explanation

Once `proto.neurons.len()` reaches `max_number_of_neurons`, every subsequent call to `add_neuron` returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` This is a permanent, application-level DoS on neuron creation for the entire SNS: no new principals can stake, no new voting power can be created, and governance is frozen for new entrants. This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS on SNS governance with concrete user and protocol harm.**

## Likelihood Explanation

Any unprivileged principal holding SNS tokens can trigger this. The attacker stakes tokens into a neuron, dissolves it, disburses it (recovering tokens minus fees), and repeats. Each cycle costs only the transaction fee and permanently consumes one neuron slot. The entry path is the public `manage_neuron` canister update call, accessible to any ingress sender. Organic long-term usage (users naturally creating and disbursing neurons over the SNS lifetime) can also exhaust the limit without any deliberate attack.

## Recommendation

`disburse_neuron` should call `remove_neuron` after the ledger transfer succeeds and both `cached_neuron_stake_e8s` and `maturity_e8s_equivalent` reach zero. Alternatively, `check_neuron_population_can_grow` should count only neurons with non-zero stake or non-zero maturity, excluding fully-disbursed zero-stake entries from the ceiling check.

## Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = N` (e.g., 10 in a PocketIC or local replica test).
2. As principal `P`, repeat `N` times:
   - Transfer SNS tokens to the governance subaccount for memo `i`.
   - Call `manage_neuron { ClaimOrRefresh { MemoAndController { memo: i, controller: P } } }` → neuron created, `proto.neurons.len() = i`.
   - Call `manage_neuron { Configure { StartDissolving } }`, advance time past dissolve delay.
   - Call `manage_neuron { Disburse { amount: None, to_account: P } }` → stake zeroed, `proto.neurons.len()` still `= i`.
3. After `N` iterations, `proto.neurons.len() = N = max_number_of_neurons`.
4. Any subsequent `ClaimOrRefresh` for a new subaccount returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` — permanently, for all principals. [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L940-975)
```rust
    fn add_neuron(&mut self, neuron: Neuron) -> Result<(), GovernanceError> {
        let neuron_id = neuron
            .id
            .as_ref()
            .expect("Neuron must have a NeuronId")
            .clone();

        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        // New neurons are not allowed when the maximum configured is reached
        self.check_neuron_population_can_grow()?;

        if self.proto.neurons.contains_key(&neuron_id.to_string()) {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Cannot add neuron. There is already a neuron with id: {neuron_id}"),
            ));
        }

        GovernanceProto::add_neuron_to_principal_to_neuron_ids_index(
            &mut self.principal_to_neuron_ids_index,
            &neuron,
        );

        add_neuron_to_function_followee_index(
            &mut self.function_followee_index,
            &self.proto.id_to_nervous_system_functions,
            &neuron,
        );

        add_neuron_to_follower_index(&mut self.topic_follower_index, &neuron);

        self.proto.neurons.insert(neuron_id.to_string(), neuron);

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L983-1006)
```rust
    fn remove_neuron(
        &mut self,
        neuron_id: &NeuronId,
        neuron: Neuron,
    ) -> Result<(), GovernanceError> {
        if !self.proto.neurons.contains_key(&neuron_id.to_string()) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!("Cannot remove neuron. Can't find a neuron with id: {neuron_id}"),
            ));
        }

        GovernanceProto::remove_neuron_from_principal_to_neuron_ids_index(
            &mut self.principal_to_neuron_ids_index,
            &neuron,
        );

        remove_neuron_from_function_followee_index(&mut self.function_followee_index, &neuron);

        remove_neuron_from_follower_index(&mut self.topic_follower_index, &neuron);

        self.proto.neurons.remove(&neuron_id.to_string());

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L1225-1236)
```rust
        let nid = id.to_string();
        let neuron = self
            .proto
            .neurons
            .get_mut(&nid)
            .expect("Expected the parent neuron to exist");

        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);

        Ok(block_height)
```

**File:** rs/sns/governance/src/governance.rs (L6365-6379)
```rust
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```
