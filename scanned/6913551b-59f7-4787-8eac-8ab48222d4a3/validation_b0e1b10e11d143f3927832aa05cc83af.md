### Title
Missing Duration Bounds Validation in SNS Swap One-Proposal Flow Allows Zero or Unbounded Swap Duration - (File: rs/nns/governance/api/src/lib.rs)

### Summary
The `swap_start_and_due_timestamps` function used in the `CreateServiceNervousSystem` one-proposal flow does not validate the `duration` field against `MIN_SALE_DURATION_SECONDS` (1 day) or `MAX_SALE_DURATION_SECONDS` (14 days). The downstream `validate_swap_due_timestamp_seconds` only checks `swap_due >= swap_start`, not that the duration is within protocol-defined bounds. This allows a `CreateServiceNervousSystem` proposal with `duration.seconds = 0` (or an arbitrarily large value) to pass all pre- and post-execution validation, resulting in SNS tokens being locked in the swap canister for zero seconds (immediate abort with no participation possible) or for an unbounded period (years), effectively freezing the SNS launch.

### Finding Description

The IC SNS launch uses a "one-proposal" flow via `CreateServiceNervousSystem`. When the NNS governance executes such a proposal, it calls `make_sns_init_payload`, which calls `swap_start_and_due_timestamps`: [1](#0-0) 

`swap_start_and_due_timestamps` extracts the raw `duration` value but performs **no bounds check**: [2](#0-1) 

The resulting `SnsInitPayload` is then validated via `validate_post_execution`, which calls `validate_swap_due_timestamp_seconds`: [3](#0-2) 

This check only enforces `swap_due >= swap_start`. It does **not** enforce:
- `swap_due - swap_start >= MIN_SALE_DURATION_SECONDS` (1 day)
- `swap_due - swap_start <= MAX_SALE_DURATION_SECONDS` (14 days)

The bounds constants and enforcement logic **do exist** in `Params::is_valid_if_initiated_at`: [4](#0-3) [5](#0-4) 

However, `is_valid_if_initiated_at` is only invoked in the **legacy** `open`-based swap flow, not in the one-proposal `SnsInitPayload` validation path. The one-proposal flow never calls it.

Additionally, at the pre-execution stage, `validate_pre_execution` only checks that `swap_start_timestamp_seconds` and `swap_due_timestamp_seconds` are absent (they are set later at execution time): [6](#0-5) 

So neither pre-execution nor post-execution validation enforces duration bounds in the one-proposal flow.

### Impact Explanation

**High.** A `CreateServiceNervousSystem` proposal submitted with `swap_parameters.duration.seconds = 0` produces `swap_due_timestamp_seconds == swap_start_timestamp_seconds`. The swap opens and is immediately due; since no participant can contribute ICP in zero seconds, the swap aborts immediately. SNS tokens allocated to the swap are locked in the swap canister until the abort is finalized and `finalize` is called. More critically, a proposer can set `duration.seconds` to an arbitrarily large value (e.g., `3_153_600_000` = 100 years). The swap would remain open for 100 years, locking all SNS tokens in the swap canister for that entire period, making the SNS non-functional and the tokens inaccessible to the project.

### Likelihood Explanation

**Low.** Exploiting this requires a `CreateServiceNervousSystem` proposal with a malformed `duration` to pass NNS governance voting. This requires either a malicious proposer whose proposal is not scrutinized by NNS voters, or a misconfigured proposal submitted in good faith. The NNS community is expected to review proposals, but there is no on-chain enforcement preventing such a proposal from being adopted.

### Recommendation

Add duration bounds validation inside `swap_start_and_due_timestamps` (or in `validate_swap_due_timestamp_seconds`) to enforce:

```rust
if duration < ONE_DAY_SECONDS {
    return Err(format!("duration ({duration}) must be >= MIN_SALE_DURATION_SECONDS ({ONE_DAY_SECONDS})"));
}
if duration > 14 * ONE_DAY_SECONDS {
    return Err(format!("duration ({duration}) must be <= MAX_SALE_DURATION_SECONDS ({})", 14 * ONE_DAY_SECONDS));
}
```

This mirrors the existing enforcement in `Params::is_valid_if_initiated_at` and closes the gap between the legacy and one-proposal flows.

### Proof of Concept

1. Construct a `CreateServiceNervousSystem` proposal with `swap_parameters.duration = Duration { seconds: Some(0) }`.
2. Submit it to NNS governance. It passes `validate_pre_execution` (duration is not checked there).
3. If the proposal is adopted, `make_sns_init_payload` calls `swap_start_and_due_timestamps` with `duration = 0`, producing `swap_due_timestamp_seconds == swap_start_timestamp_seconds`.
4. `validate_post_execution` → `validate_swap_due_timestamp_seconds` passes because `swap_due >= swap_start` (they are equal).
5. The SNS is deployed with a swap that opens and is immediately due. No participant can join. The swap aborts, and SNS tokens remain locked until `finalize` is called.
6. For the unbounded case: set `duration.seconds = Some(3_153_600_000)` (100 years). The same validation path passes. SNS tokens are locked in the swap canister for 100 years.

### Citations

**File:** rs/nns/governance/src/governance.rs (L4462-4472)
```rust
            let duration = create_service_nervous_system
                .swap_parameters
                .as_ref()
                .and_then(|swap_parameters| swap_parameters.duration);

            CreateServiceNervousSystem::swap_start_and_due_timestamps(
                start_time.unwrap_or(random_swap_start_time),
                duration.unwrap_or_default(),
                current_timestamp_seconds,
            )
        }?;
```

**File:** rs/nns/governance/api/src/lib.rs (L236-265)
```rust
        let duration = duration.seconds.ok_or("`seconds` should not be None")?;

        // TODO(NNS1-2298): we should also add 27 leap seconds to this, to avoid
        // having the swap start half a minute earlier than expected.
        let midnight_after_swap_approved_timestamp_seconds = swap_approved_timestamp_seconds
            .saturating_sub(swap_approved_timestamp_seconds % ONE_DAY_SECONDS) // floor to midnight
            .saturating_add(ONE_DAY_SECONDS); // add one day

        let swap_start_timestamp_seconds = {
            let mut possible_swap_starts = (0..2).map(|i| {
                midnight_after_swap_approved_timestamp_seconds
                    .saturating_add(ONE_DAY_SECONDS * i)
                    .saturating_add(start_time_of_day)
            });
            // Find the earliest time that's at least 24h after the swap was approved.
            possible_swap_starts
                .find(|&timestamp| timestamp > swap_approved_timestamp_seconds + ONE_DAY_SECONDS)
                .ok_or(format!(
                    "Unable to find a swap start time after the swap was approved. \
                     swap_approved_timestamp_seconds = {swap_approved_timestamp_seconds}, \
                     midnight_after_swap_approved_timestamp_seconds = {midnight_after_swap_approved_timestamp_seconds}, \
                     start_time_of_day = {start_time_of_day}, \
                     duration = {duration} \
                     This is probably a bug.",
                ))?
        };

        let swap_due_timestamp_seconds = duration
            .checked_add(swap_start_timestamp_seconds)
            .ok_or("`duration` should not be None")?;
```

**File:** rs/sns/init/src/lib.rs (L1685-1712)
```rust
    fn validate_swap_start_timestamp_seconds_pre_execution(&self) -> Result<(), String> {
        if self.swap_start_timestamp_seconds.is_none() {
            Ok(())
        } else {
            Err(format!(
                "Error: swap_start_timestamp_seconds cannot be specified pre_execution, but was {:?}",
                self.swap_start_timestamp_seconds
            ))
        }
    }

    fn validate_swap_start_timestamp_seconds(&self) -> Result<(), String> {
        match self.swap_start_timestamp_seconds {
            Some(_) => Ok(()),
            None => Err("Error: swap_start_timestamp_seconds must be specified".to_string()),
        }
    }

    fn validate_swap_due_timestamp_seconds_pre_execution(&self) -> Result<(), String> {
        if self.swap_due_timestamp_seconds.is_none() {
            Ok(())
        } else {
            Err(format!(
                "Error: swap_due_timestamp_seconds cannot be specified pre_execution, but was {:?}",
                self.swap_due_timestamp_seconds
            ))
        }
    }
```

**File:** rs/sns/init/src/lib.rs (L1714-1730)
```rust
    fn validate_swap_due_timestamp_seconds(&self) -> Result<(), String> {
        let swap_start_timestamp_seconds = self
            .swap_start_timestamp_seconds
            .ok_or("Error: swap_start_timestamp_seconds must be specified")?;

        let swap_due_timestamp_seconds = self
            .swap_due_timestamp_seconds
            .ok_or("Error: swap_due_timestamp_seconds must be specified")?;

        if swap_due_timestamp_seconds < swap_start_timestamp_seconds {
            return Err(format!(
                "Error: swap_due_timestamp_seconds({swap_due_timestamp_seconds}) must be after swap_start_timestamp_seconds({swap_start_timestamp_seconds})",
            ));
        }

        Ok(())
    }
```

**File:** rs/sns/swap/src/types.rs (L319-321)
```rust
impl Params {
    const MIN_SALE_DURATION_SECONDS: u64 = ONE_DAY_SECONDS;
    const MAX_SALE_DURATION_SECONDS: u64 = 14 * ONE_DAY_SECONDS;
```

**File:** rs/sns/swap/src/types.rs (L456-485)
```rust
    pub fn is_valid_if_initiated_at(&self, now_seconds: u64) -> Result<(), String> {
        let sale_delay_seconds = self.sale_delay_seconds.unwrap_or(0);

        let open_timestamp_seconds = now_seconds.saturating_add(sale_delay_seconds);
        let duration_seconds = self
            .swap_due_timestamp_seconds
            .saturating_sub(open_timestamp_seconds);

        if duration_seconds < Self::MIN_SALE_DURATION_SECONDS {
            return Err(format!(
                "If the swap were initiated at the requested time ({}), its duration would be \
                    {} seconds, but MIN_SALE_DURATION_SECONDS = {}.",
                now_seconds,
                duration_seconds,
                Self::MIN_SALE_DURATION_SECONDS,
            ));
        }
        // Swap can be at most MAX_SALE_DURATION_SECONDS long
        if duration_seconds > Self::MAX_SALE_DURATION_SECONDS {
            return Err(format!(
                "If the swap were initiated at the requested time ({}), its duration would be \
                    {} seconds, but MAX_SALE_DURATION_SECONDS = {}.",
                now_seconds,
                duration_seconds,
                Self::MAX_SALE_DURATION_SECONDS,
            ));
        }

        Ok(())
    }
```
