### Title
SNS Swap Finalization Permanently Blocked by Zero-Amount Neuron Basket Slots from Integer Division Truncation - (File: `rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister's `Swap::scale()` function computes each participant's SNS token allocation as `(amount_icp_e8s * total_sns_e8s) / total_icp_e8s`. When a participant's ICP contribution is small relative to the total ICP raised, this integer division truncates to a value smaller than `neuron_basket_construction_parameters.count`. The subsequent `generate_vesting_schedule()` call then produces neuron basket slots with `amount_e8s = 0`. During `sweep_sns()`, each 0-amount slot returns `TransferResult::AmountTooSmall`, which is counted as `invalid`. Since `finalize_swap()` halts and returns an error when `sweep_sns` reports any `invalid` entries, the entire swap finalization is permanently blocked — no participant receives SNS tokens and the SNS governance never receives ICP — until a manual canister upgrade intervention.

### Finding Description

The vulnerability chain spans three functions in `rs/sns/swap/src/swap.rs`:

**Step 1 — `Swap::scale()` truncates to zero or a sub-count value:** [1](#0-0) 

The formula `(amount_icp_e8s * total_sns_e8s) / total_icp_e8s` uses integer division. If `amount_icp_e8s * total_sns_e8s < total_icp_e8s`, the result is 0. Even when non-zero, if the result is less than `neuron_basket_construction_parameters.count`, the next step produces 0-amount slots.

**Step 2 — `generate_vesting_schedule()` produces 0-amount slots:** [2](#0-1) 

`apportion_approximately_equally(total_amount_e8s, count)` computes `quotient = total_amount_e8s / count`. When `total_amount_e8s < count`, `quotient = 0` and only `total_amount_e8s` slots receive 1 e8s; the remaining `count - total_amount_e8s` slots receive 0 e8s. [3](#0-2) 

**Step 3 — `transfer_helper()` returns `AmountTooSmall` for 0-amount slots:** [4](#0-3) 

**Step 4 — `sweep_sns()` counts these as `invalid`:** [5](#0-4) 

The comment explicitly acknowledges this path: *"AmountTooSmall should never happen as the sns token amount is checked in `commit`. In the case of a bug due to programmer error, increment the invalid field. This will require a manual intervention via an upgrade to correct."* However, no such check in `commit` was found that validates `scale(min_participant_icp_e8s, total_sns_e8s, total_icp_e8s) >= count`.

**Step 5 — `finalize_swap()` halts permanently:**

The test `test_finalization_halts_when_sweep_sns_fails` confirms that any `invalid` entry in `sweep_sns` causes `finalize_swap` to halt with `"Transferring SNS tokens did not complete fully, some transfers were invalid or failed. Halting swap finalization"` and sets all subsequent steps (`set_dapp_controllers`, `set_mode`, `claim_neuron`) to `None`. [6](#0-5) 

The `SnsNeuronRecipe` state is written with 0-amount slots and cannot be corrected without a canister upgrade.

### Impact Explanation

Swap finalization is permanently blocked. All direct participants and Neurons' Fund participants cannot receive their SNS tokens. The SNS governance canister never receives the ICP. The swap remains in the `COMMITTED` lifecycle state indefinitely. Recovery requires an NNS-approved canister upgrade to the Swap canister to correct the corrupted `neuron_recipes` state — a significant operational and governance burden.

### Likelihood Explanation

The condition is reachable without any privileged access. A concrete example:

- `min_participant_icp_e8s = 1_000_000` (0.01 ICP, a common minimum)
- `total_icp_e8s = 100_000_000_000_000` (1,000,000 ICP raised by many participants)
- `total_sns_e8s = 1_000_000_000` (10 SNS tokens offered)
- `count = 300` (vesting events, as seen in `rs/nervous_system/tools/release/sns_default_test_init_params_v2.yml`)
- `scale(1_000_000, 1_000_000_000, 100_000_000_000_000) = 10`
- `apportion_approximately_equally(10, 300)` → 290 slots with 0 e8s, 10 slots with 1 e8s
- 290 `invalid` entries → `finalize_swap` halts

An unprivileged user participates with the minimum ICP amount. As more participants join and `total_icp_e8s` grows, the minimum participant's scaled allocation shrinks below `count`. This mirrors the original report's scenario where a percentage-based withdrawal slowly reduces a balance to a dust amount that blocks the operation. [7](#0-6) 

### Recommendation

1. **In `create_sns_neuron_recipes()`**: After computing `amount_sns_e8s = Swap::scale(...)`, validate that `amount_sns_e8s >= neuron_basket_construction_parameters.count * sns_transaction_fee_e8s`. If not, log an error and count the participant as `invalid` (skipping recipe creation) rather than creating recipes with 0-amount slots.

2. **In swap parameter validation**: Add an invariant check that `scale(min_participant_icp_e8s, sns_token_e8s, max_icp_e8s) >= count * sns_transaction_fee_e8s` at swap open time, so the configuration is rejected before any participants join.

3. **In `finalize_swap()`**: Distinguish between `invalid` entries caused by 0-amount slots (which are unrecoverable without an upgrade) and other transient failures, and handle them separately rather than halting all finalization.

### Proof of Concept

```
Swap parameters:
  min_participant_icp_e8s = 1_000_000        // 0.01 ICP
  sns_token_e8s           = 1_000_000_000    // 10 SNS tokens
  max_icp_e8s             = 100_000_000_000_000  // 1M ICP
  count (vesting events)  = 300

Attacker action:
  Participate with amount_icp_e8s = 1_000_000 (minimum)
  Other participants fill the swap to total_icp_e8s = 100_000_000_000_000

At finalize_swap → create_sns_neuron_recipes:
  amount_sns_e8s = scale(1_000_000, 1_000_000_000, 100_000_000_000_000)
                 = (1_000_000 * 1_000_000_000) / 100_000_000_000_000
                 = 1_000_000_000_000_000 / 100_000_000_000_000
                 = 10

  generate_vesting_schedule(10, 300):
    quotient  = 10 / 300 = 0
    remainder = 10 % 300 = 10
    result    = [0]*300, last 10 incremented to 1
    → 290 slots with amount_e8s = 0, 10 slots with amount_e8s = 1

At finalize_swap → sweep_sns:
  For each of the 290 zero-amount slots:
    transfer_helper: amount(0) <= fee → AmountTooSmall → invalid += 1

  sweep_result.invalid = 290 > 0
  → finalize_swap halts: "Transferring SNS tokens did not complete fully..."
  → All participants permanently blocked from receiving SNS tokens
  → Requires NNS-approved canister upgrade to recover
```

### Citations

**File:** rs/sns/swap/src/swap.rs (L163-188)
```rust
    fn generate_vesting_schedule(
        &self,
        total_amount_e8s: u64,
    ) -> Result<Vec<ScheduledVestingEvent>, String> {
        if self.count == 0 {
            return Err(
                "NeuronBasketConstructionParameters.count must be greater than zero".to_string(),
            );
        }

        let dissolve_delay_seconds_list = (0..(self.count))
            .map(|i| i * self.dissolve_delay_interval_seconds)
            .collect::<Vec<u64>>();

        let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
        Ok(dissolve_delay_seconds_list
            .into_iter()
            .zip(chunks_e8s)
            .map(
                |(dissolve_delay_seconds, amount_e8s)| ScheduledVestingEvent {
                    dissolve_delay_seconds,
                    amount_e8s,
                },
            )
            .collect())
    }
```

**File:** rs/sns/swap/src/swap.rs (L203-212)
```rust
pub fn apportion_approximately_equally(total: u64, len: u64) -> Result<Vec<u64>, String> {
    let quotient = total
        .checked_div(len)
        .ok_or_else(|| format!("Unable to divide total={total} by len={len}"))?;
    let remainder = total % len; // For unsigned integers, % cannot overflow.

    // So far, we have only apportioned quotient * len. To reach the desired
    // total, we must still somehow add remainder (per Euclid's Division
    // Theorem). That is accomplished right after this.
    let mut result = vec![quotient; len as usize];
```

**File:** rs/sns/swap/src/swap.rs (L742-751)
```rust
    fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
        assert!(amount_icp_e8s <= u64::from(total_icp_e8s));
        // Note that the multiplication cannot overflow as both factors fit in 64 bits.
        let r = (amount_icp_e8s as u128)
            .saturating_mul(total_sns_e8s as u128)
            .div(NonZeroU128::from(total_icp_e8s));
        // This follows logically from the initial assert `amount_icp_e8s <= total_icp_e8s`.
        assert!(r <= u64::MAX as u128);
        r as u64
    }
```

**File:** rs/sns/swap/src/swap.rs (L2276-2282)
```rust
                // AmountToSmall should never happen as the sns token amount is checked in
                // `commit`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
```

**File:** rs/sns/swap/src/types.rs (L612-616)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
```

**File:** rs/sns/swap/tests/swap.rs (L2950-2972)
```rust
    assert_eq!(
        result.sweep_sns_result,
        Some(SweepResult {
            success: 2,
            skipped: 0,
            failure: 1,         // Single failed transfer
            invalid: 0,         // No invalid recipes
            global_failures: 0, // No global failures
        })
    );

    assert_eq!(
        result.error_message,
        Some(String::from(
            "Transferring SNS tokens did not complete fully, some transfers were invalid or failed. Halting swap finalization"
        ))
    );

    // Assert all other fields are set to None because finalization was halted
    assert!(result.set_dapp_controllers_call_result.is_none());
    assert!(result.set_mode_call_result.is_none());
    assert!(result.claim_neuron_result.is_none());
}
```

**File:** rs/nervous_system/tools/release/sns_default_test_init_params_v2.yml (L77-79)
```yaml
    VestingSchedule:
        events: 300
        interval: 2 seconds
```
