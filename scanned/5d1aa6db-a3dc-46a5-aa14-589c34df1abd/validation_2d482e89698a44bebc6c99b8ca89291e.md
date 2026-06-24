### Title
`created_timestamp_seconds` Not Initialized in Genesis Developer Neurons Causes Immediate Vesting Bypass - (`File: rs/sns/init/src/distributions.rs`)

---

### Summary

Developer neurons created at SNS genesis via `FractionalDeveloperVotingPower::create_neuron` are constructed with `..Default::default()`, leaving `created_timestamp_seconds` at `0`. The SNS governance `is_vesting` check computes `created_timestamp_seconds + vesting_period_seconds >= now`. Since `0 + vesting_period_seconds` is always far less than the current Unix timestamp (~1.7 billion), `is_vesting` returns `false` immediately for every genesis developer neuron, regardless of the configured `vesting_period_seconds`. All vesting-gated operations (`StartDissolving`, `Disburse`, `Split`, `IncreaseDissolveDelay`) are immediately available to the developer.

---

### Finding Description

In `rs/sns/init/src/distributions.rs`, the `create_neuron` function builds genesis developer neurons:

```rust
Ok(Neuron {
    id: Some(NeuronId { id: subaccount.to_vec() }),
    permissions: vec![permission],
    cached_neuron_stake_e8s: stake_e8s,
    followees: btreemap! {},
    dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
    voting_power_percentage_multiplier,
    vesting_period_seconds,   // correctly copied from NeuronDistribution
    ..Default::default()      // created_timestamp_seconds silently becomes 0
})
``` [1](#0-0) 

`created_timestamp_seconds` is never assigned; `Default::default()` for `u64` is `0`.

The vesting guard in `rs/sns/governance/src/neuron.rs` is:

```rust
pub fn is_vesting(&self, now: u64) -> bool {
    self.vesting_period_seconds
        .map(|vesting_period_seconds| {
            self.created_timestamp_seconds + vesting_period_seconds >= now
        })
        .unwrap_or_default()
}
``` [2](#0-1) 

With `created_timestamp_seconds = 0` and any realistic `vesting_period_seconds` (e.g., 3 years ≈ 94,608,000 s), the condition evaluates to `94,608,000 >= ~1,720,000,000`, which is `false`. `is_vesting` returns `false` for every genesis developer neuron from the moment the SNS is deployed.

The enforcement gate in `rs/sns/governance/src/governance.rs` short-circuits on `!neuron.is_vesting(...)`:

```rust
if !neuron.is_vesting(self.env.now()) {
    return Ok(());
}
``` [3](#0-2) 

This allows all otherwise-blocked commands (`StartDissolving`, `Disburse`, `Split`, `IncreaseDissolveDelay`, `SetDissolveTimestamp`) to execute immediately.

By contrast, swap neurons created via `claim_swap_neurons` correctly set `created_timestamp_seconds: now`: [4](#0-3) 

The genesis path simply omits this assignment.

---

### Impact Explanation

Any developer who controls a genesis neuron configured with a `vesting_period_seconds` (e.g., a 3-year commitment) can, immediately after SNS launch:

1. Call `manage_neuron` → `StartDissolving` — begins the dissolve countdown with no vesting delay.
2. After the dissolve delay elapses, call `manage_neuron` → `Disburse` — withdraws all staked SNS tokens to a liquid account.
3. Alternatively call `Split` to fragment and sell portions of the stake.

The vesting mechanism is intended to prove long-term commitment to SNS token holders and prevent developer token dumps. With `created_timestamp_seconds = 0`, the entire vesting schedule is silently nullified at genesis. Token holders who relied on the published vesting schedule when participating in the decentralization swap receive no protection.

---

### Likelihood Explanation

- Every SNS that configures `vesting_period_seconds` on developer neurons is affected from day one.
- The developer needs only to call the standard `manage_neuron` ingress endpoint — no privileged access, no key compromise, no governance majority required.
- The bug is silent: the neuron appears to have a vesting period configured (the field is non-zero), but the guard never fires.
- The SNS swap canister correctly sets `created_timestamp_seconds: now` for investor neurons, so the omission in the genesis path is a straightforward oversight that is easy to exploit.

---

### Recommendation

In `rs/sns/init/src/distributions.rs`, explicitly set `created_timestamp_seconds` to the SNS genesis timestamp when constructing developer neurons. The `create_neuron` function should accept a `now: u64` parameter (the canister's current time at initialization) and assign it:

```rust
Ok(Neuron {
    id: Some(NeuronId { id: subaccount.to_vec() }),
    permissions: vec![permission],
    cached_neuron_stake_e8s: stake_e8s,
    followees: btreemap! {},
    dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
    voting_power_percentage_multiplier,
    vesting_period_seconds,
    created_timestamp_seconds: now,   // <-- add this
    aging_since_timestamp_seconds: now,
    ..Default::default()
})
```

This mirrors the pattern already used in `claim_swap_neurons` and in the SNS governance `claim_neuron` path. [5](#0-4) 

---

### Proof of Concept

1. Launch an SNS with a developer neuron configured as:
   ```yaml
   vesting_period: 3 years
   dissolve_delay: 1 year
   ```
2. Immediately after genesis, call `manage_neuron` on the developer neuron with `StartDissolving`.
3. Observe that the call succeeds — no `"Neuron X is vesting and cannot call StartDissolving"` error is returned.
4. After 1 year, call `Disburse` and receive all staked SNS tokens as liquid funds, 2 years ahead of the published vesting schedule.

The root cause is confirmed by evaluating `is_vesting` with the actual stored state:
- `created_timestamp_seconds = 0` (default, never set)
- `vesting_period_seconds = 94,608,000` (3 years)
- `now ≈ 1,720,000,000`
- `0 + 94,608,000 >= 1,720,000,000` → `false` → vesting guard bypassed [1](#0-0) [2](#0-1) [6](#0-5)

### Citations

**File:** rs/sns/init/src/distributions.rs (L151-162)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L4340-4341)
```rust
            created_timestamp_seconds: now,
            aging_since_timestamp_seconds: now,
```

**File:** rs/sns/governance/src/governance.rs (L4513-4514)
```rust
                created_timestamp_seconds: now,
                aging_since_timestamp_seconds: now,
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
