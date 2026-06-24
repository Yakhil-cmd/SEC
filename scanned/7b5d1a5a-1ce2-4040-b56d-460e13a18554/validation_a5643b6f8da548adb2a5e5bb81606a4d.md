### Title
Vesting SNS Neurons Can Disburse Reward Tokens (Maturity) Without Restriction - (File: `rs/sns/governance/src/governance.rs`)

### Summary
In SNS Governance, the `check_command_is_valid_if_neuron_is_vesting` function explicitly blocks `Disburse` (stake) and `Split` for vesting neurons, but explicitly permits `DisburseMaturity`. This means a vesting neuron controller can freely transfer accumulated reward tokens (maturity) to any external account during the vesting period, bypassing the lock that vesting is designed to enforce on token outflows.

### Finding Description

The `check_command_is_valid_if_neuron_is_vesting` function in `rs/sns/governance/src/governance.rs` enforces restrictions on vesting neurons: [1](#0-0) 

`Disburse` (stake) and `Split` are blocked, but `DisburseMaturity` is explicitly allowed via the catch-all `Ok(())` arm. The `disburse_maturity` function itself performs no vesting check: [2](#0-1) 

It only checks `NeuronPermissionType::DisburseMaturity` authorization, percentage validity, and minimum amount — no check on `is_vesting`. The maturity being disbursed is the `maturity_e8s_equivalent` field, which accumulates voting rewards: [3](#0-2) 

Vesting neurons are created at SNS initialization for developer principals and are explicitly designed to lock tokens for a long-term commitment period exceeding the maximum dissolve delay: [4](#0-3) 

The `is_vesting` check confirms the neuron is locked: [5](#0-4) 

### Impact Explanation

A vesting SNS developer neuron controller holding `NeuronPermissionType::DisburseMaturity` can call `manage_neuron` with `DisburseMaturity` at any time during the vesting period and transfer all accumulated voting reward tokens (maturity) to any arbitrary account. The vesting mechanism is intended to prove long-term commitment by locking token outflows, but reward tokens are entirely unrestricted. This is a **governance authorization bug**: the vesting lock is incomplete — it covers the staked principal but not the reward stream. An SNS community that relies on vesting to ensure developer alignment is misled, as developers can continuously extract rewards while their stake appears locked.

### Likelihood Explanation

Medium. Any SNS developer neuron controller (an unprivileged ingress sender with `DisburseMaturity` permission, which is granted by default via `neuron_claimer_permissions`) can trigger this via a standard `manage_neuron` ingress call. No special privileges, governance majority, or threshold corruption is required. The only prerequisite is having a vesting neuron with accumulated maturity, which is the normal state for active SNS developer neurons.

### Recommendation

Add a vesting check inside `disburse_maturity` (or add `DisburseMaturity` to the blocked list in `check_command_is_valid_if_neuron_is_vesting`) to prevent vesting neurons from disbursing maturity until the vesting period has elapsed. Specifically, in `check_command_is_valid_if_neuron_is_vesting`:

```rust
Disburse(_) => err("Disburse"),
Split(_) => err("Split"),
DisburseMaturity(_) => err("DisburseMaturity"), // add this
```

Or, alternatively, add an `is_vesting` guard at the top of `disburse_maturity` analogous to the dissolved-state check in `disburse_neuron`.

### Proof of Concept

1. An SNS is initialized with a developer neuron having `vesting_period_seconds = 3 * ONE_YEAR_SECONDS` and `dissolve_delay_seconds = ONE_YEAR_SECONDS`.
2. The developer neuron accumulates maturity through voting rewards over time.
3. The developer calls `manage_neuron` with `Command::DisburseMaturity { percentage_to_disburse: 100, to_account: Some(<attacker_account>) }`.
4. `manage_neuron_internal` calls `check_command_is_valid_if_neuron_is_vesting` — `DisburseMaturity` falls into the `Ok(())` arm at line 4890, no error is returned.
5. `disburse_maturity` executes, deducting `maturity_e8s_equivalent` and queuing a disbursement to the attacker account with no vesting check.
6. After `MATURITY_DISBURSEMENT_DELAY_SECONDS`, the reward tokens are minted and transferred to the attacker account — all while the neuron's stake remains vesting-locked. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1616)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
```

**File:** rs/sns/governance/src/governance.rs (L1643-1651)
```rust
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;

        let maturity_to_deduct = maturity_to_deduct as u64;
```

**File:** rs/sns/governance/src/governance.rs (L1680-1698)
```rust
        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L226-234)
```text
  // The duration that this neuron is vesting.
  //
  // A neuron that is vesting is non-dissolving and cannot start dissolving until the vesting duration has elapsed.
  // Vesting can be used to lock a neuron more than the max allowed dissolve delay. This allows devs and members of
  // a particular SNS instance to prove their long-term commitment to the community. For example, the max dissolve delay
  // for a particular SNS instance might be 1 year, but the devs of the project may set their vesting duration to 3
  // years and dissolve delay to 1 year in order to prove that they are making a minimum 4 year commitment to the
  // project.
  optional uint64 vesting_period_seconds = 17;
```

**File:** rs/sns/governance/src/neuron.rs (L795-802)
```rust
    /// Returns true if this neuron is vesting, false otherwise
    pub fn is_vesting(&self, now: u64) -> bool {
        self.vesting_period_seconds
            .map(|vesting_period_seconds| {
                self.created_timestamp_seconds + vesting_period_seconds >= now
            })
            .unwrap_or_default()
    }
```
