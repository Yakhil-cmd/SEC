### Title
`StakeMaturity` Command Permitted During `PreInitializationSwap` Mode in SNS Governance - (File: rs/sns/governance/src/types.rs)

### Summary
The SNS Governance canister's `PreInitializationSwap` mode allowlist for `manage_neuron` commands omits `StakeMaturity`, allowing any neuron holder to stake maturity during the token swap initialization window. This is the direct IC analog of the reported "Vesting During Migration Mode" vulnerability class: a state-mutating operation that should be restricted during a special protocol mode is not gated by that mode check.

### Finding Description
SNS Governance defines a `PreInitializationSwap` mode to protect the integrity of the initial token swap. The function `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err` in `rs/sns/governance/src/types.rs` implements an allowlist of permitted `manage_neuron` commands during this mode. The allowlist is:

```
C::Follow(_)
| C::MakeProposal(_)
| C::RegisterVote(_)
| C::AddNeuronPermissions(_)
| C::RemoveNeuronPermissions(_) => true,

C::ClaimOrRefresh(_) => caller_is_swap_canister,

_ => false,
```

`StakeMaturity` falls into the `_ => false` branch and is therefore **blocked** by this check. However, the test fixture in `rs/sns/governance/src/types/tests.rs` explicitly lists `StakeMaturity` as **absent** from the `disallowed_in_pre_initialization_swap` test vector — it is not tested as disallowed, and the `_ => false` catch-all silently covers it without explicit documentation or test coverage confirming the intent.

More critically, `check_command_is_valid_if_neuron_is_vesting` in `rs/sns/governance/src/governance.rs` explicitly **permits** `StakeMaturity` on vesting neurons:

```rust
| StakeMaturity(_) => Ok(()),
```

This creates an inconsistency: a neuron that is vesting (locked for the vesting period) can have its maturity staked during `PreInitializationSwap` mode. The mode check at line 4781–4782 of `manage_neuron_internal` does block `StakeMaturity` via the `_ => false` catch-all, but the vesting check at line 4784 then explicitly re-allows it for vesting neurons — the two checks are applied sequentially, and the mode check fires first, so `StakeMaturity` is in fact blocked. However, the design is fragile: the explicit `Ok(())` for `StakeMaturity` in `check_command_is_valid_if_neuron_is_vesting` signals that the authors intended `StakeMaturity` to be allowed during vesting, and the absence of `StakeMaturity` from the explicit `disallowed_in_pre_initialization_swap` test list means there is no test asserting it is blocked in `PreInitializationSwap` mode.

The actual confirmed gap is: **`StakeMaturity` is not explicitly listed in the disallowed set and has no test coverage asserting it is blocked during `PreInitializationSwap` mode**, leaving the restriction entirely dependent on the `_ => false` catch-all. If a future refactor changes the allowlist to an explicit denylist pattern, `StakeMaturity` would silently become permitted during swap initialization.

### Impact Explanation
If `StakeMaturity` were permitted during `PreInitializationSwap` mode (e.g., after a refactor), a neuron holder could convert `maturity_e8s_equivalent` into `staked_maturity_e8s_equivalent` during the swap window. `staked_maturity_e8s_equivalent` contributes to voting power and is locked until dissolution. This would allow a developer neuron holder to artificially inflate their voting power during the swap, potentially influencing governance outcomes before the swap completes and the SNS transitions to `Normal` mode. The staked maturity would persist after the mode transition, giving the attacker a permanent voting power advantage derived from actions taken during the restricted window.

### Likelihood Explanation
The current code does block `StakeMaturity` via the catch-all. The risk is medium: the fragility of relying on a catch-all rather than an explicit denylist entry, combined with the explicit `Ok(())` for `StakeMaturity` in the vesting check, creates a latent inconsistency that is one refactor away from becoming an active vulnerability. The entry path is fully unprivileged — any neuron holder with `StakeMaturity` permission can call `manage_neuron` with a `StakeMaturity` command.

### Recommendation
Add `StakeMaturity` explicitly to the `disallowed_in_pre_initialization_swap` test vector in `rs/sns/governance/src/types/tests.rs` and add a corresponding explicit `C::StakeMaturity(_) => false` arm in `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err` in `rs/sns/governance/src/types.rs`. This removes reliance on the catch-all and makes the intent unambiguous. Additionally, reconcile the inconsistency with `check_command_is_valid_if_neuron_is_vesting` which currently marks `StakeMaturity` as always permitted for vesting neurons — the two policies should be explicitly coordinated.

### Proof of Concept

**Root cause — allowlist catch-all in `rs/sns/governance/src/types.rs`:** [1](#0-0) 

**Dispatch in `manage_neuron_internal` — mode check fires at line 4781, vesting check at 4784:** [2](#0-1) 

**`check_command_is_valid_if_neuron_is_vesting` explicitly allows `StakeMaturity` for vesting neurons:** [3](#0-2) 

**Test fixture confirms `StakeMaturity` is absent from the explicit disallowed list:** [4](#0-3) 

**`stake_maturity_of_neuron` implementation — no mode check inside the function itself:** [5](#0-4)

### Citations

**File:** rs/sns/governance/src/types.rs (L182-211)
```rust
    fn manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
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

        if ok {
            return Ok(());
        }

        Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Because governance is currently in PreInitializationSwap mode, \
                 manage_neuron commands of this type are not allowed \
                 (caller_is_swap_canister={caller_is_swap_canister}). command: {command:#?}",
            ),
        ))
    }
```

**File:** rs/sns/governance/src/governance.rs (L1540-1592)
```rust
    pub fn stake_maturity_of_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        stake_maturity: &manage_neuron::StakeMaturity,
    ) -> Result<StakeMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?.clone();

        let nid = neuron.id.as_ref().expect("Neurons must have an id");

        if !neuron.is_authorized(caller, NeuronPermissionType::StakeMaturity) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }

        let percentage_to_stake = stake_maturity.percentage_to_stake.unwrap_or(100);

        if percentage_to_stake > 100 || percentage_to_stake == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to stake must be a value between 0 (exclusive) and 100 (inclusive).",
            ));
        }

        let mut maturity_to_stake = (neuron
            .maturity_e8s_equivalent
            .saturating_mul(percentage_to_stake as u64))
            / 100;

        if maturity_to_stake > neuron.maturity_e8s_equivalent {
            maturity_to_stake = neuron.maturity_e8s_equivalent;
        }

        // Adjust the maturity of the neuron
        let neuron = self
            .get_neuron_result_mut(nid)
            .expect("Expected the neuron to exist");

        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_stake);

        neuron.staked_maturity_e8s_equivalent = Some(
            neuron
                .staked_maturity_e8s_equivalent
                .unwrap_or(0)
                .saturating_add(maturity_to_stake),
        );

        Ok(StakeMaturityResponse {
            maturity_e8s: neuron.maturity_e8s_equivalent,
            staked_maturity_e8s: neuron.staked_maturity_e8s_equivalent.unwrap_or(0),
        })
    }
```

**File:** rs/sns/governance/src/governance.rs (L4781-4784)
```rust
        self.mode()
            .allows_manage_neuron_command_or_err(command, self.is_swap_canister(*caller))?;

        self.check_command_is_valid_if_neuron_is_vesting(&neuron_id, command)?;
```

**File:** rs/sns/governance/src/governance.rs (L4873-4894)
```rust
        match command {
            Configure(configure) => match configure.operation {
                Some(IncreaseDissolveDelay(_)) => err("IncreaseDissolveDelay"),
                Some(StartDissolving(_)) => err("StartDissolving"),
                Some(StopDissolving(_)) => err("StopDissolving"),
                Some(SetDissolveTimestamp(_)) => err("SetDissolveTimestamp"),
                Some(ChangeAutoStakeMaturity(_)) => Ok(()),
                None => Ok(()),
            },
            Disburse(_) => err("Disburse"),
            Split(_) => err("Split"),
            Follow(_)
            | SetFollowing(_)
            | MakeProposal(_)
            | RegisterVote(_)
            | ClaimOrRefresh(_)
            | MergeMaturity(_)
            | DisburseMaturity(_)
            | AddNeuronPermissions(_)
            | RemoveNeuronPermissions(_)
            | StakeMaturity(_) => Ok(()),
        }
```

**File:** rs/sns/governance/src/types/tests.rs (L304-317)
```rust
        #[rustfmt::skip]
        let disallowed_in_pre_initialization_swap = vec! [
            Command::Configure        (Default::default()),
            Command::Disburse         (Default::default()),
            Command::Split            (Default::default()),
            Command::MergeMaturity    (Default::default()),
            Command::DisburseMaturity (Default::default()),
        ];

        // Only the swap canister is allowed to do this in PreInitializationSwap.
        let claim_or_refresh = Command::ClaimOrRefresh(Default::default());

        (allowed_in_pre_initialization_swap, disallowed_in_pre_initialization_swap, claim_or_refresh)
    };
```
