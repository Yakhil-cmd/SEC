### Title
SNS Governance Neuron Population Limit Counts Fully-Disbursed Zero-Stake Neurons, Enabling Permanent DoS on New Neuron Creation - (`rs/sns/governance/src/governance.rs`)

### Summary

`check_neuron_population_can_grow` in SNS Governance counts **all** entries in `proto.neurons` — including neurons whose stake has been fully disbursed to zero — against `max_number_of_neurons`. Because `disburse_neuron` never removes the neuron record from `proto.neurons`, the global neuron count monotonically increases. Once the limit is reached, no new neurons can ever be created in the SNS, permanently denying participation in governance.

### Finding Description

`check_neuron_population_can_grow` enforces the neuron population ceiling by reading `self.proto.neurons.len()`: [1](#0-0) 

`self.proto.neurons` is a `BTreeMap<String, Neuron>` that holds every neuron ever created in the SNS, regardless of its current stake. [2](#0-1) 

`disburse_neuron` transfers the neuron's tokens to the caller and then only decrements `cached_neuron_stake_e8s` in-place; it does **not** call `remove_neuron` or delete the entry from `proto.neurons`: [3](#0-2) 

`remove_neuron` exists and correctly removes the entry plus all index entries: [4](#0-3) 

But it is never invoked from `disburse_neuron`. As a result, every create-then-disburse cycle permanently increments `proto.neurons.len()` by one. `add_neuron`, which is called for every new neuron claim, always passes through `check_neuron_population_can_grow` first: [5](#0-4) 

The `max_number_of_neurons` parameter is documented as "When this maximum is reached, no new neurons will be created **until some are removed**": [6](#0-5) 

But there is no user-facing command to remove a neuron, and no automatic garbage-collection path for zero-stake neurons in SNS Governance (unlike NNS Governance, which tracks `garbage_collectable_neurons_count`). The "until some are removed" escape hatch is therefore unreachable in practice.

### Impact Explanation

Once `proto.neurons.len()` reaches `max_number_of_neurons`, every call to `add_neuron` — triggered by `claim_or_refresh` for a new subaccount, `split_neuron`, or `claim_swap_neurons` — returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` This is a **permanent, irreversible denial of service** on neuron creation for the entire SNS: no new participants can stake, no new voting power can be created, and the SNS governance effectively freezes for new entrants.

### Likelihood Explanation

The attack is reachable by any unprivileged principal who can hold SNS tokens. The attacker stakes tokens into a neuron, dissolves it, disburses it (recovering the tokens minus fees), and repeats. Each cycle costs only the transaction fee and permanently consumes one slot. With a default `max_number_of_neurons` ceiling, a well-funded attacker or even organic long-term usage (users naturally create and disburse neurons over the SNS lifetime) will eventually exhaust the limit. The entry path is the public `manage_neuron` canister update call, which is fully accessible to any ingress sender.

### Recommendation

`disburse_neuron` should call `remove_neuron` after the ledger transfer succeeds and `cached_neuron_stake_e8s` reaches zero (and `maturity_e8s_equivalent` is also zero). Alternatively, `check_neuron_population_can_grow` should count only neurons with non-zero stake or non-zero maturity, mirroring the "alive" vs. "ever-owned" distinction described in the reference report.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = N` (e.g., 10 for a test SNS).
2. As principal `P`, repeat N times:
   a. Transfer SNS tokens to the governance subaccount for memo `i`.
   b. Call `manage_neuron { ClaimOrRefresh { MemoAndController { memo: i, controller: P } } }` → neuron created, `proto.neurons.len()` = `i`.
   c. Call `manage_neuron { Configure { StartDissolving } }`, advance time past dissolve delay.
   d. Call `manage_neuron { Disburse { amount: None, to_account: P } }` → stake zeroed, but `proto.neurons.len()` still = `i`.
3. After N iterations, `proto.neurons.len()` = N = `max_number_of_neurons`.
4. Any subsequent `ClaimOrRefresh` for a new subaccount returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` — permanently, for all principals. [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1987-1991)
```rust
pub struct Governance {
    /// The current set of neurons registered in governance as a map from
    /// neuron IDs to neurons.
    #[prost(btree_map = "string, message", tag = "1")]
    pub neurons: ::prost::alloc::collections::BTreeMap<::prost::alloc::string::String, Neuron>,
```
