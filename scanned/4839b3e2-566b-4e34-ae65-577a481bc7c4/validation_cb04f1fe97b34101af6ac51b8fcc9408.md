### Title
Missing Swap Duration Bounds Validation in `CreateServiceNervousSystem` Proposal Allows SNS Tokens to Become Stuck - (File: `rs/nns/governance/api/src/lib.rs`, `rs/sns/init/src/lib.rs`)

### Summary
The `CreateServiceNervousSystem` NNS governance proposal path does not validate that the swap `duration` field falls within the min/max bounds enforced by the SNS swap canister. A proposal with `duration = 0` (or an arbitrarily large duration) passes all NNS governance validation checks, gets adopted, executes SNS creation, and deposits SNS tokens into the swap canister — but the swap canister then refuses to open because `Params::is_valid_if_initiated_at` rejects the duration. The SNS tokens become permanently stuck in the swap canister with no automatic recovery path.

### Finding Description

The validation gap spans three layers:

**Layer 1 — Proposal submission validation** (`rs/nns/governance/src/governance.rs:5037`): `validate_create_service_nervous_system` calls `SnsInitPayload::try_from(...)` which invokes `validate_pre_execution()`. At this stage, `swap_start_timestamp_seconds` and `swap_due_timestamp_seconds` are not yet set (they are `None` pre-execution), so no duration bounds check is possible. [1](#0-0) 

**Layer 2 — Proposal execution** (`rs/nns/governance/src/governance.rs:4462`): `make_sns_init_payload` extracts `duration` from `swap_parameters` and calls `swap_start_and_due_timestamps`. If `duration` is `None`, it falls back to `Duration::default()` (i.e., `seconds: None`), which would error. But if `duration = Some(Duration { seconds: Some(0) })`, it passes through silently. [2](#0-1) 

**`swap_start_and_due_timestamps`** (`rs/nns/governance/api/src/lib.rs:228`) extracts `duration.seconds` and computes `swap_due_timestamp_seconds = duration + swap_start_timestamp_seconds` with no min/max bounds check on `duration`: [3](#0-2) 

The resulting `SnsInitPayload` then passes `validate_post_execution()`, which calls `validate_swap_due_timestamp_seconds`: [4](#0-3) 

This only checks `swap_due_timestamp_seconds >= swap_start_timestamp_seconds`. With `duration = 0`, `due == start`, so this check passes.

**Layer 3 — Swap canister open** (`rs/sns/swap/src/types.rs:456`): `Params::is_valid_if_initiated_at` enforces `MIN_SALE_DURATION_SECONDS = ONE_DAY_SECONDS` and `MAX_SALE_DURATION_SECONDS = 14 * ONE_DAY_SECONDS`. With `duration = 0`, `duration_seconds < MIN_SALE_DURATION_SECONDS` and the swap refuses to open: [5](#0-4) 

The swap canister state machine is `PENDING → ADOPTED → OPEN → COMMITTED/ABORTED`. If the swap cannot transition from `ADOPTED` to `OPEN`, it stays in `ADOPTED` indefinitely. The SNS tokens deposited in the swap canister have no automatic return path. [6](#0-5) 

### Impact Explanation

**Impact: High.** SNS tokens (potentially millions of ICP-equivalent value) deposited into the swap canister become permanently stuck. The swap canister cannot transition to `OPEN`, `COMMITTED`, or `ABORTED`. The fallback controller mechanism exists for failed swaps, but only applies to swaps that have been opened and aborted — not swaps stuck in `ADOPTED` state. The SNS governance canister and treasury are also deployed but non-functional, as the decentralization swap never completes.

### Likelihood Explanation

**Likelihood: Low.** Exploiting this requires an NNS governance proposal with `duration = 0` (or an extreme value) to be adopted by NNS majority vote. This is analogous to the external report's "configuration error from the admin." In practice, NNS voters review proposals, but the duration field is buried in the `SwapParameters` sub-message and could be overlooked. The absence of a proposal-time bounds check means there is no automated guard to catch this before adoption.

### Recommendation

1. In `swap_start_and_due_timestamps` (`rs/nns/governance/api/src/lib.rs`), add bounds validation on `duration` against `Params::MIN_SALE_DURATION_SECONDS` and `Params::MAX_SALE_DURATION_SECONDS` before computing timestamps.

2. In `validate_swap_due_timestamp_seconds` (`rs/sns/init/src/lib.rs`), add a check that `swap_due_timestamp_seconds - swap_start_timestamp_seconds` is within `[MIN_SALE_DURATION_SECONDS, MAX_SALE_DURATION_SECONDS]`.

3. In `validate_create_service_nervous_system` (`rs/nns/governance/src/governance.rs`), simulate the timestamp computation and call `Params::is_valid_if_initiated_at` at proposal submission time so invalid durations are rejected before the proposal reaches a vote.

### Proof of Concept

1. An NNS neuron holder submits a `CreateServiceNervousSystem` proposal with `swap_parameters.duration = Some(Duration { seconds: Some(0) })`.
2. `validate_create_service_nervous_system` calls `SnsInitPayload::try_from(...)` → `validate_pre_execution()`. Timestamps are `None` pre-execution, so `validate_swap_due_timestamp_seconds` is not called. Proposal is accepted for voting.
3. NNS governance adopts the proposal (e.g., due to voter inattention to the duration field).
4. `do_create_service_nervous_system` → `make_sns_init_payload` calls `swap_start_and_due_timestamps(start_time, Duration { seconds: Some(0) }, now)`. This computes `swap_due_timestamp_seconds = swap_start_timestamp_seconds + 0 = swap_start_timestamp_seconds`.
5. `validate_post_execution()` → `validate_swap_due_timestamp_seconds`: `due >= start` → passes (equal).
6. SNS is deployed; SNS tokens are transferred to the swap canister. Swap enters `ADOPTED` state.
7. Heartbeat triggers `open` on the swap canister. `Params::is_valid_if_initiated_at(now)` computes `duration_seconds = swap_due_timestamp_seconds - now ≈ 0 < MIN_SALE_DURATION_SECONDS (86400)` → returns `Err(...)`. Swap cannot open.
8. Swap remains in `ADOPTED` state indefinitely. SNS tokens are stuck. [7](#0-6) [8](#0-7)

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

**File:** rs/nns/governance/src/governance.rs (L5037-5051)
```rust
    fn validate_create_service_nervous_system(
        &self,
        create_service_nervous_system: &CreateServiceNervousSystem,
    ) -> Result<(), GovernanceError> {
        // Must be able to convert to a valid SnsInitPayload.
        let conversion_result = SnsInitPayload::try_from(ApiCreateServiceNervousSystem::from(
            create_service_nervous_system.clone(),
        ));

        let validated = conversion_result.map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Invalid CreateServiceNervousSystem: {e}"),
            )
        })?;
```

**File:** rs/nns/governance/api/src/lib.rs (L228-268)
```rust
    pub fn swap_start_and_due_timestamps(
        start_time_of_day: GlobalTimeOfDay,
        duration: Duration,
        swap_approved_timestamp_seconds: u64,
    ) -> Result<(u64, u64), String> {
        let start_time_of_day = start_time_of_day
            .seconds_after_utc_midnight
            .ok_or("`seconds_after_utc_midnight` should not be None")?;
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

        Ok((swap_start_timestamp_seconds, swap_due_timestamp_seconds))
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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1-30)
```rust
// This file is @generated by prost-build.
/// The `swap` canister smart contract is used to perform a type of
/// single-price auction (SNS/ICP) of one token type SNS for another token
/// type ICP (this is typically ICP, but can be treated as a variable) at a
/// specific date/time in the future.
///
/// Such a single-price auction is typically used to decentralize an SNS,
/// i.e., to ensure that a sufficient number of governance tokens of the
/// SNS are distributed among different participants.
///
/// State (lifecycle) diagram for the swap canister's state.
///
/// ```text
///                                                                      sufficient_participation
///                                                                      && (swap_due || icp_target_reached)
/// PENDING -------------------> ADOPTED ---------------------> OPEN -----------------------------------------> COMMITTED
///          Swap receives a request        The opening delay      |                                                |
///          from NNS governance to         has elapsed            | not sufficient_participation                   |
///          schedule opening                                      | && (swap_due || icp_target_reached)            |
///                                                                v                                                v
///                                                             ABORTED ---------------------------------------> <DELETED>
/// ```
///
/// Here `sufficient_participation` means that the minimum number of
/// participants `min_participants` has been reached, each contributing
/// between `min_participant_icp_e8s` and `max_participant_icp_e8s`, and
/// their total contributions add up to at least `min_icp_e8s` and at most
/// `max_icp_e8s`.
///
/// `icp_target_reached` means that the total amount of ICP contributed is
```
