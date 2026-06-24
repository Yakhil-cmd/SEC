### Title
Developer Neurons Retain Unilateral Voting Power During `PreInitializationSwap` Phase Before Swap Participants Receive Neurons - (File: `rs/sns/governance/src/types.rs`)

### Summary
SNS developer neurons are created at genesis with a non-zero `voting_power_percentage_multiplier` and are explicitly permitted to submit proposals and cast votes while the SNS is in `PreInitializationSwap` mode. Because swap participants hold no neurons during this phase, developer neurons collectively hold 100% of the realized voting power and can unilaterally adopt governance proposals—including `UpgradeSnsToNextVersion`, `ManageLedgerParameters`, `AddGenericNervousSystemFunction`, and `ManageDappCanisterSettings`—before a single swap participant receives a neuron.

### Finding Description

**Token distribution at genesis.**
`FractionalDeveloperVotingPower::get_initial_neurons` creates developer neurons with:

```
voting_power_percentage_multiplier =
    (swap.initial_swap_amount_e8s * 100) / swap.total_e8s
``` [1](#0-0) 

When `initial_swap_amount_e8s == total_e8s` (a valid and common configuration where the entire swap bucket is offered in the first round), the multiplier equals **100**, giving developer neurons full, unreduced voting power from the moment the SNS is deployed.

**Proposals and votes are allowed in `PreInitializationSwap` mode.**
`Mode::allows_manage_neuron_command_or_err` explicitly permits `MakeProposal` and `RegisterVote` in `PreInitializationSwap` mode: [2](#0-1) 

**The disallowed-proposal list is incomplete.**
`functions_disallowed_in_pre_initialization_swap` blocks six specific actions but leaves many high-impact actions unrestricted: [3](#0-2) 

Notably absent from the blocklist: `UpgradeSnsToNextVersion`, `AdvanceTargetVersion`, `ManageLedgerParameters`, `AddGenericNervousSystemFunction`, `ManageSnsMetadata`, and `ManageDappCanisterSettings`.

**Ballot computation does not filter by swap phase.**
`compute_ballots_for_new_proposal` iterates over every neuron in `self.proto.neurons` without any guard for the governance mode or for whether the neuron is a developer neuron: [4](#0-3) 

Because swap-participant neurons are only minted after `finalize` is called on the swap canister (via `claim_swap_neurons`), the electoral roll during the swap phase contains **only** developer neurons. Developer neurons therefore hold 100% of the realized voting power regardless of the configured multiplier.

**Swap participants receive neurons only after finalization.**
`claim_swap_neurons` is called by the swap canister after the swap commits: [5](#0-4) 

Until that call completes, no swap-participant neuron exists in governance state.

### Impact Explanation

A developer who controls the developer neurons can, during the open swap window:

1. Submit an `UpgradeSnsToNextVersion` proposal (not blocked by the disallowed list). Although the upgrade binary is determined by the NNS SNS-W canister, adopting this proposal mid-swap can change the governance canister's behavior before any swap participant has a neuron or any ability to oppose the vote.
2. Submit a `ManageLedgerParameters` proposal to alter transaction fees, affecting the economics of the swap itself.
3. Submit an `AddGenericNervousSystemFunction` proposal to register a new callable function, expanding the attack surface for subsequent proposals.
4. Submit a `ManageDappCanisterSettings` proposal to alter settings of dapp canisters that swap participants believe they are investing in.

In all cases the proposal passes with 100% of the voting power because no opposing neurons exist yet. Swap participants who contributed ICP have no recourse until after finalization.

### Likelihood Explanation

The entry path requires a developer who intentionally deploys an SNS and exploits the window between swap open and swap finalization. This is a semi-trusted role, but the entire value proposition of the decentralization swap is that swap participants should be protected from developer unilateralism. The configuration `initial_swap_amount_e8s == total_e8s` is the default example in the SNS documentation and is therefore common. The attack window is bounded by the swap duration (days to weeks), giving ample time to act. Likelihood is **medium**.

### Recommendation

1. Expand `functions_disallowed_in_pre_initialization_swap` to include `UpgradeSnsToNextVersion`, `AdvanceTargetVersion`, `ManageLedgerParameters`, `AddGenericNervousSystemFunction`, and `ManageDappCanisterSettings`.
2. Alternatively, disallow `MakeProposal` entirely in `PreInitializationSwap` mode and defer all governance activity until after the swap finalizes and swap-participant neurons exist.
3. At minimum, add prominent documentation warning SNS deployers and swap participants that developer neurons hold unilateral voting power during the swap window.

### Proof of Concept

1. Developer deploys an SNS with `initial_swap_amount_e8s == total_e8s`. Developer neurons receive `voting_power_percentage_multiplier = 100`.
2. The swap opens (`Lifecycle::Open`). Swap participants transfer ICP but no participant neurons exist in SNS governance yet.
3. Developer calls `manage_neuron` with `MakeProposal { action: UpgradeSnsToNextVersion {} }`. The mode check in `allows_manage_neuron_command_or_err` passes (MakeProposal is allowed). The action check in `allows_proposal_action_or_err` passes (UpgradeSnsToNextVersion is not in the disallowed list).
4. `compute_ballots_for_new_proposal` builds an electoral roll containing only developer neurons. The proposal is adopted immediately because developer neurons hold 100% of the voting power.
5. The SNS governance canister is upgraded before the swap finalizes and before any swap participant has a neuron or any ability to vote against the change. [6](#0-5) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/init/src/distributions.rs (L59-81)
```rust
        // Multiplying this way will give the developer_voting_power_percentage_multiplier
        // as a percentage while also allowing use of checked_div.
        let developer_voting_power_percentage_multiplier = ((swap.initial_swap_amount_e8s as u128)
            * 100)
            .checked_div(swap.total_e8s as u128)
            .expect(
                "Underflow detected when calculating developer voting power percentage multiplier",
            ) as u64;

        let mut initial_neurons = btreemap! {};

        for developer_neuron_distribution in developer_neurons {
            let neuron = self.create_neuron(
                developer_neuron_distribution,
                developer_voting_power_percentage_multiplier,
                parameters,
            )?;

            initial_neurons.insert(neuron.id.as_ref().unwrap().to_string(), neuron);
        }

        Ok(initial_neurons)
    }
```

**File:** rs/sns/governance/src/types.rs (L186-197)
```rust
        use manage_neuron::Command as C;
        let ok = match command {
            C::Follow(_)
            | C::MakeProposal(_)
            | C::RegisterVote(_)
            | C::AddNeuronPermissions(_)
            | C::RemoveNeuronPermissions(_) => true,

            C::ClaimOrRefresh(_) => caller_is_swap_canister,

            _ => false,
        };
```

**File:** rs/sns/governance/src/types.rs (L253-262)
```rust
    pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
        vec![
            NervousSystemFunction::manage_nervous_system_parameters(),
            NervousSystemFunction::transfer_sns_treasury_funds(),
            NervousSystemFunction::mint_sns_tokens(),
            NervousSystemFunction::upgrade_sns_controlled_canister(),
            NervousSystemFunction::register_dapp_canisters(),
            NervousSystemFunction::deregister_dapp_canisters(),
        ]
    }
```

**File:** rs/sns/governance/src/governance.rs (L4431-4440)
```rust
    pub fn claim_swap_neurons(
        &mut self,
        request: ClaimSwapNeuronsRequest,
        caller_principal_id: PrincipalId,
    ) -> ClaimSwapNeuronsResponse {
        let now = self.env.now();

        if !self.is_swap_canister(caller_principal_id) {
            return ClaimSwapNeuronsResponse::new_with_error(ClaimSwapNeuronsError::Unauthorized);
        }
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```
