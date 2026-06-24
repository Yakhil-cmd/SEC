### Title
Neurons' Fund Neuron Can Avoid SNS Swap Maturity Draw by Leaving and Rejoining the Community Fund - (File: rs/nns/governance/src/neuron/types.rs)

### Summary
A Neurons' Fund neuron controller can observe a pending `CreateServiceNervousSystem` proposal during its public voting period, call `LeaveCommunityFund` before the proposal executes, avoid having their maturity drawn for the SNS swap, and immediately rejoin the fund afterward. There is no timelock or cooldown on leaving or rejoining the Neurons' Fund. This shifts a disproportionate maturity burden onto the remaining Neurons' Fund participants.

### Finding Description
When a `CreateServiceNervousSystem` proposal is adopted, the NNS Governance canister executes `do_create_service_nervous_system`, which calls `draw_maturity_from_neurons_fund`. This function calls `self.neuron_store.list_active_neurons_fund_neurons()` to enumerate only the neurons **currently** in the Neurons' Fund at execution time, then draws maturity proportionally from each. [1](#0-0) 

The snapshot is taken at proposal execution time, not at proposal submission time. Because NNS proposals are public and have a multi-day voting period, any neuron controller can observe a pending proposal, predict its outcome, and call `LeaveCommunityFund` before execution. [2](#0-1) 

There is no timelock, cooldown, or minimum membership duration enforced in `leave_community_fund`. Immediately after the proposal executes, the neuron controller can call `JoinCommunityFund` again with no restriction. [3](#0-2) 

The maturity draw is proportional: each neuron's share is `neuron_maturity / total_fund_maturity * intended_participation`. When a large neuron exits, the remaining neurons' proportional shares increase, meaning they each contribute more maturity per unit of their own stake. [4](#0-3) 

### Impact Explanation
A neuron controller can repeatedly avoid contributing maturity to SNS swaps while remaining in the Neurons' Fund between swaps to continue accruing voting rewards. The remaining Neurons' Fund neurons bear a larger share of the maturity draw than they should, constituting an unfair redistribution of cost. This is a **governance accounting / ledger conservation bug**: the Neurons' Fund's total contribution to the swap is reduced (or the per-neuron burden on remaining participants is increased), violating the fairness invariant of the matched-funding mechanism.

### Likelihood Explanation
The attack requires no privileged access. NNS proposals are fully public, have a voting period of days, and the `manage_neuron` call to `LeaveCommunityFund` is a standard unprivileged ingress message callable by any neuron controller. The attacker only needs to monitor the NNS proposal queue and submit two `manage_neuron` calls (leave before execution, rejoin after). This is straightforward to automate.

### Recommendation
Implement a minimum membership duration before a neuron's `LeaveCommunityFund` takes effect with respect to pending `CreateServiceNervousSystem` proposals. Concretely, either:
1. Take the Neurons' Fund snapshot at **proposal submission time** (not execution time) and lock participating neurons' membership for the duration of the swap, or
2. Enforce a timelock (e.g., 1–7 days) on `LeaveCommunityFund` so that a neuron cannot exit and immediately avoid a pending draw.

### Proof of Concept
1. A `CreateServiceNervousSystem` proposal with `neurons_fund_participation = true` is submitted to NNS Governance.
2. During the voting period, attacker (a Neurons' Fund neuron controller) calls `manage_neuron` with `Command::Configure(Configure { operation: Some(Operation::LeaveCommunityFund(LeaveCommunityFund {})) })`.
3. The proposal reaches majority and is executed. `do_create_service_nervous_system` calls `draw_maturity_from_neurons_fund`, which calls `list_active_neurons_fund_neurons()`. The attacker's neuron is absent; its maturity is not drawn.
4. Attacker immediately calls `manage_neuron` with `Operation::JoinCommunityFund(JoinCommunityFund {})` to rejoin.
5. The remaining Neurons' Fund neurons each bear a larger proportional share of the maturity drawn for the swap. The attacker's neuron contributed nothing while retaining full membership benefits before and after. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L4418-4432)
```rust
        let (initial_neurons_fund_participation_snapshot, neurons_fund_participation_constraints) =
            if swap_parameters.neurons_fund_participation.unwrap_or(false) {
                let (
                    initial_neurons_fund_participation_snapshot,
                    neurons_fund_participation_constraints,
                ) = self
                    .draw_maturity_from_neurons_fund(&proposal_id, create_service_nervous_system)?;
                (
                    initial_neurons_fund_participation_snapshot,
                    Some(neurons_fund_participation_constraints),
                )
            } else {
                self.record_neurons_fund_participation_not_requested(&proposal_id)?;
                (NeuronsFundSnapshot::empty(), None)
            };
```

**File:** rs/nns/governance/src/governance.rs (L7381-7386)
```rust
        let neurons_fund = self.neuron_store.list_active_neurons_fund_neurons();
        let initial_neurons_fund_participation = PolynomialNeuronsFundParticipation::new(
            neurons_fund_participation_limits,
            swap_participation_limits,
            neurons_fund,
        )?;
```

**File:** rs/nns/governance/src/governance.rs (L7416-7417)
```rust
        self.neuron_store
            .draw_maturity_from_neurons_fund(&initial_neurons_fund_participation_snapshot)?;
```

**File:** rs/nns/governance/src/neuron/types.rs (L599-609)
```rust
    /// Join the Internet Computer's Neurons' Fund. If this neuron is
    /// already a member of the Neurons' Fund, an error is returned.
    fn join_community_fund(&mut self, now_seconds: u64) -> Result<(), GovernanceError> {
        if self.joined_community_fund_timestamp_seconds.unwrap_or(0) == 0 {
            self.joined_community_fund_timestamp_seconds = Some(now_seconds);
            Ok(())
        } else {
            // Already joined...
            Err(GovernanceError::new(ErrorType::AlreadyJoinedCommunityFund))
        }
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L611-620)
```rust
    /// Leave the Internet Computer's Neurons' Fund. If this neuron is not a
    /// member of the Neurons' Fund, an error will be returned.
    fn leave_community_fund(&mut self) -> Result<(), GovernanceError> {
        if self.joined_community_fund_timestamp_seconds.unwrap_or(0) != 0 {
            self.joined_community_fund_timestamp_seconds = None;
            Ok(())
        } else {
            Err(GovernanceError::new(ErrorType::NotInTheCommunityFund))
        }
    }
```

**File:** rs/nns/governance/src/neurons_fund.rs (L1094-1107)
```rust
                    // Division is safe, as `total_maturity_equivalent_icp_e8s != 0` in this branch.
                    let proportion_to_overall_neurons_fund = u64_to_dec(maturity_equivalent_icp_e8s)?
                        .checked_div(total_maturity_equivalent_icp_e8s)
                        .ok_or_else(|| {
                            "NeuronsFundParticipation cannot be created due to division error."
                                .to_string()
                        })?;
                    // Multiplication is safe because the left factor is a value between 0.0 and 1.0.
                    let ideal_participation_amount_icp_e8s = proportion_to_overall_neurons_fund
                        .checked_mul(intended_neurons_fund_participation_icp_e8s)
                        .ok_or_else(|| {
                            "NeuronsFundParticipation cannot be created due to multiplication error."
                                .to_string()
                        })?;
```
