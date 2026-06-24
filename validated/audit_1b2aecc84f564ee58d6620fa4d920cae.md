Audit Report

## Title
`created_timestamp_seconds` Not Initialized in Genesis Developer Neurons Causes Immediate Vesting Bypass - (File: rs/sns/init/src/distributions.rs)

## Summary
The `create_neuron` function in `rs/sns/init/src/distributions.rs` constructs genesis developer neurons using `..Default::default()`, which leaves `created_timestamp_seconds` at `0`. The `is_vesting` check in `rs/sns/governance/src/neuron.rs` evaluates `created_timestamp_seconds + vesting_period_seconds >= now`; with `created_timestamp_seconds = 0`, this condition is always `false` against any real Unix timestamp, so every genesis developer neuron is treated as fully vested from the moment the SNS launches. All vesting-gated operations (`StartDissolving`, `Disburse`, `Split`, `IncreaseDissolveDelay`) are immediately available to the developer, regardless of the configured `vesting_period_seconds`.

## Finding Description
In `rs/sns/init/src/distributions.rs` at lines 151–162, `create_neuron` builds the genesis neuron struct:

```rust
Ok(Neuron {
    id: Some(NeuronId { id: subaccount.to_vec() }),
    permissions: vec![permission],
    cached_neuron_stake_e8s: stake_e8s,
    followees: btreemap! {},
    dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
    voting_power_percentage_multiplier,
    vesting_period_seconds,   // set from NeuronDistribution
    ..Default::default()      // created_timestamp_seconds silently becomes 0
})
```

A grep over the entire file confirms `created_timestamp_seconds` is never assigned anywhere in `distributions.rs`. `Default::default()` for `u64` is `0`.

The vesting guard in `rs/sns/governance/src/neuron.rs` at lines 795–802:

```rust
pub fn is_vesting(&self, now: u64) -> bool {
    self.vesting_period_seconds
        .map(|vesting_period_seconds| {
            self.created_timestamp_seconds + vesting_period_seconds >= now
        })
        .unwrap_or_default()
}
```

With `created_timestamp_seconds = 0` and `vesting_period_seconds = 94,608,000` (3 years), the expression evaluates to `94,608,000 >= ~1,720,000,000`, which is `false`. `is_vesting` returns `false` for every genesis developer neuron.

The enforcement gate in `rs/sns/governance/src/governance.rs` at lines 4862–4864 short-circuits on `!is_vesting`:

```rust
if !neuron.is_vesting(self.env.now()) {
    return Ok(());
}
```

This bypasses all subsequent error branches for `StartDissolving`, `Disburse`, `Split`, `IncreaseDissolveDelay`, and `SetDissolveTimestamp`. By contrast, swap neurons created via `claim_swap_neurons` at lines 4507–4529 explicitly set `created_timestamp_seconds: now`, confirming the genesis path omission is a straightforward oversight.

## Impact Explanation
A developer controlling a genesis neuron configured with any `vesting_period_seconds` can immediately call `StartDissolving` to begin the dissolve countdown, then `Disburse` after the dissolve delay to withdraw all staked SNS tokens as liquid funds — entirely bypassing the published vesting schedule. This constitutes unauthorized access to governance-controlled developer neuron funds: the vesting mechanism is the sole protocol-level commitment preventing developer token dumps, and it is silently nullified at genesis for every affected SNS. This matches the **High** bounty impact: unauthorized access to neurons/governance assets where the developer can extract funds ahead of schedule with no special privileges beyond controlling their own neuron key.

## Likelihood Explanation
Every SNS that configures `vesting_period_seconds` on developer neurons is affected from day one. The developer needs only to call the standard `manage_neuron` ingress endpoint — no privileged access, no governance majority, no key compromise required. The bug is silent: the neuron struct stores a non-zero `vesting_period_seconds`, giving the appearance of a vesting schedule, while the guard never fires. The exploit is repeatable across every SNS deployment using developer vesting.

## Recommendation
In `rs/sns/init/src/distributions.rs`, pass the SNS genesis timestamp into `create_neuron` and explicitly assign it:

```rust
fn create_neuron(
    &self,
    neuron_distribution: &NeuronDistribution,
    voting_power_percentage_multiplier: u64,
    parameters: &NervousSystemParameters,
    now: u64,   // <-- add genesis timestamp
) -> Result<Neuron, String> {
    // ...
    Ok(Neuron {
        // ...
        vesting_period_seconds,
        created_timestamp_seconds: now,       // <-- add this
        aging_since_timestamp_seconds: now,   // <-- add this
        ..Default::default()
    })
}
```

This mirrors the pattern already used in `claim_swap_neurons` (lines 4513–4514) and in the `claim_or_refresh_neuron` path (lines 4340–4341).

## Proof of Concept
1. Deploy an SNS with a developer neuron configured with `vesting_period: 3 years` and `dissolve_delay: 1 year`.
2. Immediately after genesis, call `manage_neuron` on the developer neuron with command `StartDissolving`.
3. Observe the call succeeds — no `"Neuron X is vesting and cannot call StartDissolving"` error is returned.
4. After 1 year (the dissolve delay), call `Disburse` and receive all staked SNS tokens as liquid funds, 2 years ahead of the published vesting schedule.

Root cause can be verified deterministically in a unit test by constructing a `Neuron` via `FractionalDeveloperVotingPower::create_neuron`, asserting `created_timestamp_seconds == 0`, and then calling `is_vesting(now)` with a realistic `now` (~1,720,000,000) to confirm it returns `false` despite a non-zero `vesting_period_seconds`.