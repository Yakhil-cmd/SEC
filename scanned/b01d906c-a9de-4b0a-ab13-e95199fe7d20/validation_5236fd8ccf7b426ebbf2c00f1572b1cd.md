### Title
Off-by-one in `swap_start_and_due_timestamps` forces unnecessary 24-hour delay when swap is approved at exact `start_time_of_day` boundary - (File: `rs/nns/governance/api/src/lib.rs`)

---

### Summary

`swap_start_and_due_timestamps` is called during `CreateServiceNervousSystem` proposal execution to compute when an SNS token swap will open. It searches for the earliest candidate start time that is "at least 24h after the swap was approved." However, the comparison uses `>` (strictly greater than) instead of `>=`, so when the proposal is adopted at exactly the same time of day as the configured `start_time_of_day`, the natural 24h boundary is rejected and the swap is forced to open 48h after approval instead of 24h — an unnecessary one-day dead period.

---

### Finding Description

In `rs/nns/governance/api/src/lib.rs`, `swap_start_and_due_timestamps` builds two candidate swap-start timestamps (one day apart) and picks the first one that satisfies the 24h minimum:

```rust
// Find the earliest time that's at least 24h after the swap was approved.
possible_swap_starts
    .find(|&timestamp| timestamp > swap_approved_timestamp_seconds + ONE_DAY_SECONDS)
``` [1](#0-0) 

The comment says **"at least 24h"** (i.e., `>=`), but the code uses `>` (strictly greater than). The two candidates are:

- **i=0**: `midnight_after_approval + start_time_of_day`
- **i=1**: `midnight_after_approval + ONE_DAY + start_time_of_day`

When `swap_approved_timestamp_seconds % ONE_DAY_SECONDS == start_time_of_day`, candidate i=0 equals exactly `swap_approved_timestamp_seconds + ONE_DAY_SECONDS`. The `>` check rejects it, so candidate i=1 (48h after approval) is used instead.

The same function is re-exported and called from `rs/nns/governance/src/proposals/create_service_nervous_system.rs` and invoked during `make_sns_init_payload` at proposal execution time: [2](#0-1) 

The integration tests only assert `>= now + ONE_DAY_SECONDS` without covering the exact-boundary edge case: [3](#0-2) 

---

### Impact Explanation

When a `CreateServiceNervousSystem` proposal is adopted at exactly the same time of day as the SNS's configured `start_time_of_day`, the swap opening is delayed by a full 24 hours beyond the natural boundary. During this window no swap participation is possible, directly harming the SNS launch: reduced community engagement, lower participation rates, and potential loss of momentum for the token sale. For high-profile SNS launches where the voting period ends at a predictable time, this is a concrete and avoidable dead period.

---

### Likelihood Explanation

The trigger condition is `swap_approved_timestamp_seconds % ONE_DAY_SECONDS == start_time_of_day`. This occurs whenever the NNS governance proposal is adopted at exactly the same time of day as the configured swap start time. Any NNS neuron holder can submit a `CreateServiceNervousSystem` proposal; the adoption timestamp is determined by the governance voting process and is not under attacker control, but the condition arises naturally — especially for SNS projects that configure a popular start time (e.g., 12:00 UTC) and whose proposal voting period ends predictably. No privileged access is required.

---

### Recommendation

Change `>` to `>=` on the boundary check to match the stated intent ("at least 24h after the swap was approved"):

```rust
// Before (buggy):
.find(|&timestamp| timestamp > swap_approved_timestamp_seconds + ONE_DAY_SECONDS)

// After (fixed):
.find(|&timestamp| timestamp >= swap_approved_timestamp_seconds + ONE_DAY_SECONDS)
``` [4](#0-3) 

---

### Proof of Concept

**Setup:**
- `start_time_of_day` = 43200 (12:00 PM UTC)
- `swap_approved_timestamp_seconds` = 129600 (12:00 PM UTC on day 2 since epoch)
- `swap_approved_timestamp_seconds % ONE_DAY_SECONDS` = 43200 = `start_time_of_day` ← boundary condition

**Computation:**
- `midnight_after_swap_approved_timestamp_seconds` = 129600 − 43200 + 86400 = **172800** (midnight of day 3)
- Candidate i=0: 172800 + 43200 = **216000** (12:00 PM day 3, exactly 24h after approval)
- Check: `216000 > 129600 + 86400` → `216000 > 216000` → **FALSE** → rejected
- Candidate i=1: 172800 + 86400 + 43200 = **302400** (12:00 PM day 4, 48h after approval)
- Check: `302400 > 216000` → **TRUE** → accepted

**Result:** The swap opens at 302400 (48h after approval) instead of 216000 (24h after approval), creating a mandatory 24h dead period during which no swap participation is possible.

### Citations

**File:** rs/nns/governance/api/src/lib.rs (L250-261)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L4467-4472)
```rust
            CreateServiceNervousSystem::swap_start_and_due_timestamps(
                start_time.unwrap_or(random_swap_start_time),
                duration.unwrap_or_default(),
                current_timestamp_seconds,
            )
        }?;
```

**File:** rs/sns/integration_tests/src/initialization_flow.rs (L1285-1290)
```rust
    assert!(
        get_lifecycle_response
            .decentralization_sale_open_timestamp_seconds
            .unwrap()
            >= now + ONE_DAY_SECONDS
    );
```
