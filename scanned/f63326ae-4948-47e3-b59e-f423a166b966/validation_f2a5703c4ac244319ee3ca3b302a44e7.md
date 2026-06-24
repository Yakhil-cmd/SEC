### Title
SNS Developer Neuron Vesting Bypass via Unset `created_timestamp_seconds` (Zero) — (File: `rs/sns/init/src/distributions.rs`)

---

### Summary

The IC's analog to the "TGE" value is `created_timestamp_seconds` on SNS neurons. All vesting enforcement in SNS governance is anchored to this field. Developer neurons created at SNS genesis are constructed with `created_timestamp_seconds` left at its protobuf default of `0`, because the `create_neuron` helper uses `..Default::default()` without explicitly setting this field. The `is_vesting` predicate then computes `created_timestamp_seconds + vesting_period_seconds >= now`, which evaluates to a timestamp in 1970–1973 for any realistic vesting period, making the comparison always false against a real-world `now`. Every developer neuron with a `vesting_period_seconds` is therefore immediately treated as non-vesting, and the vesting lock is silently bypassed from the moment the SNS launches.

---

### Finding Description

**Root cause — `create_neuron` in `rs/sns/init/src/distributions.rs`:**

```rust
Ok(Neuron {
    id: Some(NeuronId { id: subaccount.to_vec() }),
    permissions: vec![permission],
    cached_neuron_stake_e8s: stake_e8s,
    followees: btreemap! {},
    dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
    voting_power_percentage_multiplier,
    vesting_period_seconds,
    ..Default::default()   // ← created_timestamp_seconds = 0
})
``` [1](#0-0) 

`created_timestamp_seconds` is never assigned; it silently inherits the `u64` default of `0`.

**Vesting predicate — `is_vesting` in `rs/sns/governance/src/neuron.rs`:**

```rust
pub fn is_vesting(&self, now: u64) -> bool {
    self.vesting_period_seconds
        .map(|vesting_period_seconds| {
            self.created_timestamp_seconds + vesting_period_seconds >= now
        })
        .unwrap_or_default()
}
``` [2](#0-1) 

With `created_timestamp_seconds = 0` and `vesting_period_seconds = Some(3 * ONE_YEAR_SECONDS ≈ 94_608_000)`, the expression evaluates to `94_608_000 >= ~1_720_000_000` → **false**. The neuron is never considered vesting.

**Enforcement gate — `check_command_is_valid_if_neuron_is_vesting` in `rs/sns/governance/src/governance.rs`:**

```rust
if !neuron.is_vesting(self.env.now()) {
    return Ok(());
}
``` [3](#0-2) 

Because `is_vesting` returns `false`, the gate is never entered, and `StartDissolving`, `Disburse`, and `Split` are all permitted immediately.

**Neuron proto field — `vesting_period_seconds` is optional but `created_timestamp_seconds` is not validated:** [4](#0-3) 

---

### Impact Explanation

Developer neurons are intended to be locked for their full `vesting_period_seconds` before they can dissolve or disburse. Because `created_timestamp_seconds = 0`, the vesting window is computed relative to Unix epoch and has already expired by the time the SNS launches. A developer neuron controller can immediately call `manage_neuron` with `StartDissolving` or `Disburse` and extract all staked SNS tokens, defeating the commitment guarantee that the vesting mechanism is designed to provide. This undermines investor confidence and the economic security of every SNS launched via the one-proposal flow.

---

### Likelihood Explanation

Every SNS developer neuron that specifies a non-zero `vesting_period_seconds` is affected. The code path is exercised on every SNS launch. The developer neuron controller is an unprivileged ingress sender who simply calls the public `manage_neuron` endpoint. No special access, key material, or governance majority is required. The bypass is automatic and silent — no error is raised.

---

### Recommendation

Explicitly set `created_timestamp_seconds` to the canister's current time (`env.now()` or the equivalent initialization timestamp) inside `create_neuron` in `rs/sns/init/src/distributions.rs`. Additionally, add a validation guard in the SNS governance initialization path that rejects any neuron whose `vesting_period_seconds` is `Some(_)` but whose `created_timestamp_seconds` is `0`, analogous to the non-zero TGE check recommended in the external report.

---

### Proof of Concept

1. Submit a `CreateServiceNervousSystem` NNS proposal with a developer neuron configured as:
   ```
   vesting_period_seconds: Some(3 * ONE_YEAR_SECONDS)
   ```
2. After the SNS launches, the developer neuron is stored with `created_timestamp_seconds = 0` (default from `..Default::default()`).
3. Call `manage_neuron` → `Configure` → `StartDissolving` on the developer neuron.
4. `check_command_is_valid_if_neuron_is_vesting` calls `is_vesting(now)`:
   - `0 + 94_608_000 >= 1_720_000_000` → `false`
   - Gate returns `Ok(())` immediately.
5. The neuron begins dissolving. After the dissolve delay, call `Disburse` to extract all tokens — the 3-year vesting lock is completely bypassed. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/init/src/distributions.rs (L118-163)
```rust
    /// Create a neuron available at genesis
    fn create_neuron(
        &self,
        neuron_distribution: &NeuronDistribution,
        voting_power_percentage_multiplier: u64,
        parameters: &NervousSystemParameters,
    ) -> Result<Neuron, String> {
        let (
            principal_id,
            stake_e8s,
            subaccount_memo,
            dissolve_delay_seconds,
            vesting_period_seconds,
        ) = (
            neuron_distribution.controller()?,
            neuron_distribution.stake_e8s,
            neuron_distribution.memo,
            neuron_distribution.dissolve_delay_seconds,
            neuron_distribution.vesting_period_seconds,
        );

        let subaccount = compute_neuron_staking_subaccount(principal_id, subaccount_memo);

        let permission = NeuronPermission {
            principal: Some(principal_id),
            permission_type: parameters
                .neuron_claimer_permissions
                .as_ref()
                .expect("NervousSystemParameters.neuron_claimer_permissions must be present")
                .permissions
                .clone(),
        };

        Ok(Neuron {
            id: Some(NeuronId {
                id: subaccount.to_vec(),
            }),
            permissions: vec![permission],
            cached_neuron_stake_e8s: stake_e8s,
            followees: btreemap! {},
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
            voting_power_percentage_multiplier,
            vesting_period_seconds,
            ..Default::default()
        })
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

**File:** rs/sns/governance/src/governance.rs (L4844-4895)
```rust
    /// Returns an error if the given neuron is vesting and the given command cannot be called by
    /// a vesting neuron
    fn check_command_is_valid_if_neuron_is_vesting(
        &self,
        neuron_id: &NeuronId,
        command: &manage_neuron::Command,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::{Command::*, configure::Operation::*};

        // If this is a "claim" call, the neuron doesn't exist yet, so we return (because no checks
        // can be made). A "refresh" call can be made on a vesting neuron, so in this case also
        // results in returning Ok.
        if let ClaimOrRefresh(_) = command {
            return Ok(());
        }

        let neuron = self.get_neuron_result(neuron_id)?;

        if !neuron.is_vesting(self.env.now()) {
            return Ok(());
        }

        let err = |op: &str| -> Result<(), GovernanceError> {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {neuron_id} is vesting and cannot call {op}"),
            ))
        };

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
