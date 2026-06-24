### Title
Missing `MergeMaturity` Vesting Guard Allows Stake Inflation on Vesting SNS Neurons - (File: rs/sns/governance/src/governance.rs)

### Summary

The `check_command_is_valid_if_neuron_is_vesting` function in the SNS Governance canister explicitly permits `MergeMaturity` on vesting neurons. This is the analog of the external report's pattern: a flag/guard that should restrict an operation on a locked/vesting asset is absent, allowing an operation that should be blocked. Specifically, a vesting neuron is designed to be locked (non-dissolving, cannot start dissolving, cannot disburse, cannot split), yet `MergeMaturity` — which permanently converts accumulated maturity into staked tokens, increasing the neuron's `cached_neuron_stake_e8s` — is explicitly allowed. This inflates the neuron's stake and voting power during the vesting period, which is contrary to the intent of the vesting lock.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the function `check_command_is_valid_if_neuron_is_vesting` is called from `manage_neuron_internal` before dispatching any neuron command. It blocks `Disburse`, `Split`, and several `Configure` sub-operations on vesting neurons, but explicitly returns `Ok(())` for `MergeMaturity`:

```rust
Follow(_)
| SetFollowing(_)
| MakeProposal(_)
| RegisterVote(_)
| ClaimOrRefresh(_)
| MergeMaturity(_)       // <-- allowed on vesting neurons
| DisburseMaturity(_)
| AddNeuronPermissions(_)
| RemoveNeuronPermissions(_)
| StakeMaturity(_) => Ok(()),
```

`MergeMaturity` merges accumulated maturity into the neuron's staked balance, permanently increasing `cached_neuron_stake_e8s`. This is a one-way, irreversible operation that increases the neuron's stake and voting power during the vesting period. The vesting mechanism is intended to lock the neuron's economic position (no dissolving, no disbursing, no splitting), but `MergeMaturity` allows the neuron's stake to grow unboundedly during vesting.

The analog to the external report is direct: just as `isLockedCollateral` was removed from `LendingPool.liquidate()` making it impossible to liquidate non-locked collateral, here the vesting guard is missing for `MergeMaturity`, making it possible to perform a stake-modifying operation on a locked (vesting) neuron that should be restricted.

### Impact Explanation

An unprivileged ingress caller who controls a vesting SNS developer neuron (a realistic scenario — developer neurons are created at SNS genesis with `vesting_period_seconds` set) can call `ManageNeuron::MergeMaturity` repeatedly during the vesting period to merge all accumulated maturity into stake. This:

1. **Inflates voting power** during the vesting period beyond what was intended at SNS genesis, potentially allowing a developer to gain disproportionate governance control before their commitment period has elapsed.
2. **Permanently converts maturity to stake** — once merged, the maturity cannot be unmerged. This bypasses the economic intent of the vesting lock.
3. **Undermines the SNS governance security model** — the vesting period is specifically designed to prove long-term commitment; allowing stake inflation during vesting weakens this guarantee for all SNS token holders.

### Likelihood Explanation

High. Any SNS with developer neurons that have `vesting_period_seconds` set is affected. The `MergeMaturity` command is a standard, publicly documented neuron operation. Any developer neuron controller can call it at any time via the standard `manage_neuron` ingress endpoint. No special privileges, admin keys, or threshold attacks are required. The entry path is a direct ingress call to the SNS Governance canister's `manage_neuron` method.

### Recommendation

Add `MergeMaturity` to the list of blocked commands for vesting neurons in `check_command_is_valid_if_neuron_is_vesting`. The corrected match arm should be:

```rust
MergeMaturity(_) => err("MergeMaturity"),
```

This is consistent with the existing treatment of `Disburse` and `Split`, which are blocked because they modify the neuron's stake or economic position during vesting.

### Proof of Concept

1. An SNS is initialized with a developer neuron having `vesting_period_seconds = Some(3 * ONE_YEAR_SECONDS)` and `dissolve_delay_seconds = ONE_YEAR_SECONDS`.
2. The developer neuron accumulates maturity through voting rewards over time.
3. During the vesting period, the developer calls `manage_neuron` with `Command::MergeMaturity { percentage_to_merge: 100 }`.
4. `manage_neuron_internal` calls `check_command_is_valid_if_neuron_is_vesting`, which returns `Ok(())` for `MergeMaturity`.
5. `merge_maturity` executes, converting all maturity into `cached_neuron_stake_e8s`, increasing the neuron's stake and voting power.
6. This can be repeated each time new maturity accumulates, throughout the entire vesting period.

**Root cause lines:** [1](#0-0) 

**Entry point:** [2](#0-1) 

**Vesting check definition:** [3](#0-2) 

**Vesting period field on SNS neuron:** [4](#0-3) 

**Developer neuron vesting period at genesis:** [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4781-4784)
```rust
        self.mode()
            .allows_manage_neuron_command_or_err(command, self.is_swap_canister(*caller))?;

        self.check_command_is_valid_if_neuron_is_vesting(&neuron_id, command)?;
```

**File:** rs/sns/governance/src/governance.rs (L4873-4895)
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
    }
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

**File:** rs/sns/init/proto/ic_sns_init/pb/v1/sns_init.proto (L320-328)
```text
  // The duration that this neuron is vesting.
  //
  // A neuron that is vesting is non-dissolving and cannot start dissolving until the vesting duration has elapsed.
  // Vesting can be used to lock a neuron more than the max allowed dissolve delay. This allows devs and members of
  // a particular SNS instance to prove their long-term commitment to the community. For example, the max dissolve delay
  // for a particular SNS instance might be 1 year, but the devs of the project may set their vesting duration to 3
  // years and dissolve delay to 1 year in order to prove that they are making a minimum 4 year commitment to the
  // project.
  optional uint64 vesting_period_seconds = 5;
```
