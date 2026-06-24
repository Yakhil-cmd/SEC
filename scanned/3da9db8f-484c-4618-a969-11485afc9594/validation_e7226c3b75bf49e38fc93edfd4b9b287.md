### Title
Unchecked Triple-Multiplication Overflow in SNS Neuron `voting_power()` Causes Governance DoS or Silent Voting Power Corruption - (File: rs/sns/governance/src/neuron.rs)

---

### Summary

The `voting_power()` function in SNS governance performs unchecked multiplication of three `u128` values derived from `u64` governance parameters and neuron state. If `max_dissolve_delay_bonus_percentage` or `max_age_bonus_percentage` are set to large values (no enforced upper bound is present in the function), the intermediate triple-product overflows `u128`, causing either silent voting power corruption or a canister trap via internal `assert!` statements, bricking proposal creation for all users.

---

### Finding Description

In `rs/sns/governance/src/neuron.rs`, the `voting_power()` function computes the dissolve-delay bonus and age bonus using plain `*` operators on `u128` values:

```rust
// dissolve delay bonus
(stake * d * max_dissolve_delay_bonus_percentage as u128)
    / (100 * max_dissolve_delay_seconds as u128)

// age bonus
(d_stake * a * max_age_bonus_percentage as u128)
    / (100 * max_neuron_age_for_age_bonus as u128)
``` [1](#0-0) 

All three factors in each product are `u128` values cast from `u64` fields. The maximum product of three `u64::MAX` values is approximately `(1.84 × 10^19)^3 ≈ 6.2 × 10^57`, which far exceeds `u128::MAX ≈ 3.4 × 10^38`. Even with two factors at `u64::MAX`, the product `stake * d` alone approaches `u128::MAX`.

Immediately after each multiplication, the function has `assert!` statements (not `debug_assert!`, so they are active in release builds):

```rust
assert!(d_stake <= stake + (stake * (max_dissolve_delay_bonus_percentage as u128) / 100));
assert!(ad_stake <= d_stake + (d_stake * (max_age_bonus_percentage as u128) / 100));
``` [2](#0-1) 

If the triple-product wraps to a small value while the assert's right-hand side (`stake * bonus`) also wraps differently, the inequality can be violated, causing the canister to trap.

The `max_dissolve_delay_bonus_percentage` and `max_age_bonus_percentage` fields are `uint64` in the protobuf schema with no enforced upper bound visible in the `voting_power()` function itself: [3](#0-2) 

The default values are 100 and 25 respectively, but the fields accept any `u64` value. The SNS init validation enforces ceilings on `initial_reward_rate_basis_points` but no analogous ceiling was found for these bonus percentage fields. [4](#0-3) 

The vulnerable function is called from `compute_ballots_for_new_proposal()`, which iterates over **all** neurons when any user submits a proposal: [5](#0-4) 

---

### Impact Explanation

If the `assert!` fires for any single neuron during `compute_ballots_for_new_proposal()`, the entire call traps. Because the function iterates over all neurons, one neuron with a stake/dissolve-delay combination that triggers overflow under extreme governance parameters causes proposal creation to fail for **all** users — a complete governance DoS. If the overflow wraps silently without triggering the assert, voting power is silently underreported, corrupting governance outcomes (proposals passing or failing incorrectly based on wrong tallies).

---

### Likelihood Explanation

The overflow requires `max_dissolve_delay_bonus_percentage` to be set to a large value. An SNS creator sets initial parameters at deployment with no enforced ceiling on this field. A concrete example: with `max_dissolve_delay_bonus_percentage = u64::MAX ≈ 1.84 × 10^19`, a neuron holding 100 ICP (`stake = 10^10 e8s`) with a dissolve delay of ~31 years (`d = 10^9 s`) produces:

```
stake * d * bonus = 10^10 × 10^9 × 1.84×10^19 ≈ 1.84×10^38 ≈ u128::MAX
```

This is right at the overflow boundary. Any neuron with these parameters would trigger the bug. Because SNS creators supply initial `NervousSystemParameters` without an enforced ceiling on this field, the attack surface is reachable at SNS genesis without requiring a governance majority after the fact.

---

### Recommendation

1. **Enforce an upper bound** on `max_dissolve_delay_bonus_percentage` and `max_age_bonus_percentage` in SNS init validation (analogous to the ceiling on `initial_reward_rate_basis_points`).
2. **Replace unchecked `*` with `checked_mul`** in `voting_power()` and return a saturated or error result instead of allowing wrapping:
   ```rust
   stake.checked_mul(d)
       .and_then(|v| v.checked_mul(max_dissolve_delay_bonus_percentage as u128))
       .map(|v| v / denominator)
       .unwrap_or(u64::MAX as u128)
   ```
3. **Reorder operations** to divide before multiplying where possible (e.g., compute `(stake / max_dissolve_delay_seconds) * d * bonus / 100`) to reduce intermediate magnitude.

---

### Proof of Concept

1. Deploy an SNS with `max_dissolve_delay_bonus_percentage = u64::MAX` in `NervousSystemParameters`.
2. Create a neuron with `cached_neuron_stake_e8s = 10_000_000_000` (100 ICP) and `dissolve_delay_seconds = 1_000_000_000` (~31 years).
3. Any principal submits a governance proposal, triggering `compute_ballots_for_new_proposal()`.
4. Inside the loop, `voting_power()` is called for the neuron above.
5. `

### Citations

**File:** rs/sns/governance/src/neuron.rs (L213-233)
```rust
        let d_stake = stake
            + if max_dissolve_delay_seconds > 0 {
                (stake * d * max_dissolve_delay_bonus_percentage as u128)
                    / (100 * max_dissolve_delay_seconds as u128)
            } else {
                0
            };
        // Sanity check.
        assert!(d_stake <= stake + (stake * (max_dissolve_delay_bonus_percentage as u128) / 100));
        // The voting power is also a function of the age of the
        // neuron, giving a bonus of up to max_age_bonus_percentage at max_neuron_age_for_age_bonus.
        let a = std::cmp::min(self.age_seconds(now_seconds), max_neuron_age_for_age_bonus) as u128;
        let ad_stake = d_stake
            + if max_neuron_age_for_age_bonus > 0 {
                (d_stake * a * max_age_bonus_percentage as u128)
                    / (100 * max_neuron_age_for_age_bonus as u128)
            } else {
                0
            };
        // Final stake 'ad_stake' has is not more than max_age_bonus_percentage above 'd_stake'.
        assert!(ad_stake <= d_stake + (d_stake * (max_age_bonus_percentage as u128) / 100));
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1240-1246)
```text
  optional uint64 max_dissolve_delay_bonus_percentage = 20;

  // Analogous to the previous field (see the previous comment),
  // but this one relates to neuron age instead of dissolve delay.
  //
  // To achieve functionality equivalent to NNS, this should be set to 25.
  optional uint64 max_age_bonus_percentage = 21;
```

**File:** rs/sns/init/src/lib.rs (L1175-1188)
```rust
    fn validate_initial_reward_rate_basis_points(&self) -> Result<(), String> {
        let initial_reward_rate_basis_points = self
            .initial_reward_rate_basis_points
            .ok_or("Error: initial_reward_rate_basis_points must be specified")?;
        if initial_reward_rate_basis_points
            > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING
        {
            Err(format!(
                "Error: initial_reward_rate_basis_points must be less than or equal to {}",
                VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING
            ))
        } else {
            Ok(())
        }
```

**File:** rs/sns/governance/src/governance.rs (L5255-5270)
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

```
