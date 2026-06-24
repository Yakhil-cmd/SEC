### Title
Developer Neuron Voting Power Silently Zeroed by Integer Truncation in `get_initial_neurons` - (File: `rs/sns/init/src/distributions.rs`)

---

### Summary

In `FractionalDeveloperVotingPower::get_initial_neurons()`, the `developer_voting_power_percentage_multiplier` is computed via integer (floor) division. When `initial_swap_amount_e8s * 100 < total_e8s` — i.e., the initial swap round is less than 1% of total swap tokens — the result truncates silently to `0`. Every developer neuron then receives `voting_power_percentage_multiplier = 0`, giving them **zero voting power** at SNS genesis. The `validate()` function does not catch this edge case, so the misconfiguration passes all pre-execution checks.

---

### Finding Description

`get_initial_neurons` computes the multiplier as:

```rust
let developer_voting_power_percentage_multiplier = ((swap.initial_swap_amount_e8s as u128)
    * 100)
    .checked_div(swap.total_e8s as u128)
    .expect(
        "Underflow detected when calculating developer voting power percentage multiplier",
    ) as u64;
``` [1](#0-0) 

This is integer floor division. If `initial_swap_amount_e8s * 100 < total_e8s`, the quotient truncates to `0`. The computed `0` is then applied uniformly to every developer neuron:

```rust
let neuron = self.create_neuron(
    developer_neuron_distribution,
    developer_voting_power_percentage_multiplier,  // 0
    parameters,
)?;
``` [2](#0-1) 

The `validate()` function only enforces:

```rust
if swap_distribution.initial_swap_amount_e8s == 0 { ... }
if swap_distribution.total_e8s < swap_distribution.initial_swap_amount_e8s { ... }
``` [3](#0-2) 

There is **no check** that the computed multiplier is non-zero. A configuration such as `initial_swap_amount_e8s = 1` and `total_e8s = 200` passes all validation but produces a multiplier of `0`.

---

### Impact Explanation

The SNS neuron `voting_power` function applies the multiplier as:

```rust
let v = self.voting_power_percentage_multiplier as u128;
let vad_stake = ad_stake
    .checked_mul(v)
    ...
    .checked_div(100)
    ...;
``` [4](#0-3) 

When `v = 0`, `vad_stake = 0` regardless of stake, dissolve delay, or age bonus. The neuron documentation explicitly states: "0 will result in 0 voting power." [5](#0-4) 

Consequence: **all developer neurons have zero voting power at SNS genesis**. Developers cannot vote on proposals, cannot earn voting rewards, and have no governance influence — even though they hold tokens. The SNS is effectively controlled entirely by swap participants from day one, contrary to the `FractionalDeveloperVotingPower` design intent, which is to give developers proportional (non-zero) voting power scaled by `initial_swap_amount_e8s / total_e8s`.

---

### Likelihood Explanation

The condition `initial_swap_amount_e8s * 100 < total_e8s` (i.e., initial swap < 1% of total swap tokens) is a realistic and common SNS configuration. An SNS project planning to sell tokens across multiple rounds may set a small initial swap amount relative to the total. The `CreateServiceNervousSystem` NNS proposal path passes through `SnsInitPayload::try_from` → `FractionalDeveloperVotingPower::validate()`, which does not catch this. [6](#0-5) 

An SNS creator submitting such a proposal via NNS governance would unknowingly deploy an SNS where all developer neurons have zero voting power. No privileged access or key compromise is required — only a valid (but edge-case) parameter configuration.

---

### Recommendation

1. **Add a post-computation check** in `get_initial_neurons` (or in `validate()`) that rejects configurations where the computed multiplier would be `0`:

```rust
if developer_voting_power_percentage_multiplier == 0 {
    return Err("initial_swap_amount_e8s is too small relative to total_e8s: \
                developer voting power multiplier would be zero".to_string());
}
```

2. Alternatively, use **ceiling division** instead of floor division so that any non-zero `initial_swap_amount_e8s` yields at least `1%`.

3. Add the equivalent guard to `FractionalDeveloperVotingPower::validate()` so it is caught at proposal submission time, not only at execution time.

---

### Proof of Concept

**Configuration**:
- `swap_distribution.initial_swap_amount_e8s = 1`
- `swap_distribution.total_e8s = 200`

**Step 1** — `validate()` passes:
- `1 > 0` ✓
- `200 >= 1` ✓

**Step 2** — `get_initial_neurons()` computes:
```
developer_voting_power_percentage_multiplier = (1 * 100) / 200 = 0
```

**Step 3** — Every developer neuron is created with `voting_power_percentage_multiplier = 0`. [7](#0-6) 

**Step 4** — `voting_power()` returns `0` for all developer neurons:
```
vad_stake = ad_stake * 0 / 100 = 0
```

Developer neurons hold tokens but have no governance influence at SNS genesis. The bug is analogous to the AFiManager `updateProportion` case where the fail-safe correction silently zeros out the last element's proportion, rendering it inactive.

### Citations

**File:** rs/sns/init/src/distributions.rs (L61-66)
```rust
        let developer_voting_power_percentage_multiplier = ((swap.initial_swap_amount_e8s as u128)
            * 100)
            .checked_div(swap.total_e8s as u128)
            .expect(
                "Underflow detected when calculating developer voting power percentage multiplier",
            ) as u64;
```

**File:** rs/sns/init/src/distributions.rs (L70-77)
```rust
        for developer_neuron_distribution in developer_neurons {
            let neuron = self.create_neuron(
                developer_neuron_distribution,
                developer_voting_power_percentage_multiplier,
                parameters,
            )?;

            initial_neurons.insert(neuron.id.as_ref().unwrap().to_string(), neuron);
```

**File:** rs/sns/init/src/distributions.rs (L104-113)
```rust
        if swap_distribution.initial_swap_amount_e8s == 0 {
            return Err(
                "Error: swap_distribution.initial_swap_amount_e8s must be greater than 0"
                    .to_string(),
            );
        }

        if swap_distribution.total_e8s < swap_distribution.initial_swap_amount_e8s {
            return Err("Error: swap_distribution.total_e8s must be greater than or equal to swap_distribution.initial_swap_amount_e8s".to_string());
        }
```

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

**File:** rs/sns/governance/src/neuron.rs (L189-192)
```rust
    /// - The voting power multiplier depends on the neuron.voting_power_percentage_multiplier,
    ///   and is applied against the total voting power of the neuron. It is represented
    ///   as a percent in the range of 0 and 100 where 0 will result in 0 voting power,
    ///   and 100 will result in unadjusted voting power.
```

**File:** rs/sns/governance/src/neuron.rs (L237-245)
```rust
        let v = self.voting_power_percentage_multiplier as u128;

        // Apply the multiplier to 'ad_stake' and divide by 100 to have the same effect as
        // multiplying by a percent.
        let vad_stake = ad_stake
            .checked_mul(v)
            .expect("Overflow detected when calculating voting power")
            .checked_div(100)
            .expect("Underflow detected when calculating voting power");
```

**File:** rs/sns/init/src/lib.rs (L988-998)
```rust
    fn validate_token_distribution(&self) -> Result<(), String> {
        let initial_token_distribution = self
            .initial_token_distribution
            .as_ref()
            .ok_or_else(|| "Error: initial-token-distribution must be specified".to_string())?;

        let nervous_system_parameters = self.get_nervous_system_parameters();

        match initial_token_distribution {
            FractionalDeveloperVotingPower(f) => f.validate(&nervous_system_parameters)?,
        }
```
