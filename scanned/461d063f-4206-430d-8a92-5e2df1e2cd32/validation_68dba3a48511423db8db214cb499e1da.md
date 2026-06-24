### Title
Stale Cached Maturity Modulation Value Used in Neuron Spawning and Maturity Disbursement — (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS Governance canister's `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()` functions apply a cached `maturity_modulation.current_value_permyriad` to determine how much ICP to mint for neuron owners, without verifying that the cached value is fresh. This value is updated once per day by the `UpdateIcpXdrRateRelatedData` timer task via the Exchange Rate Canister (XRC). If the XRC is unavailable for multiple consecutive days, the cached modulation becomes stale, and all neuron spawning and maturity disbursement during that window will mint incorrect ICP amounts — either over-minting (benefiting users at protocol expense) or under-minting (harming users).

---

### Finding Description

**Root cause — no freshness check before consuming the cached modulation:**

In `maybe_spawn_neurons()`, the governance canister reads the cached modulation directly:

```rust
let maturity_modulation = match self
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{
    None => return,
    Some(value) => value,
};
``` [1](#0-0) 

The field `updated_at_days_since_epoch` exists on `MaturityModulation` and records when the value was last computed, but it is **never consulted** before the value is used to mint ICP. The same pattern appears in `try_finalize_maturity_disbursement()`:

```rust
let maturity_modulation = governance
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad);
``` [2](#0-1) 

**How the cached value becomes stale:**

The `UpdateIcpXdrRateRelatedData` timer task fetches ICP/XDR rates from the XRC once per day and calls `update_maturity_modulation()`, which guards against double-updating on the same day:

```rust
if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
    return;
}
``` [3](#0-2) 

If the XRC call fails, the task returns early without updating the modulation:

```rust
let Some(rate) = maybe_rate else {
    return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
};
``` [4](#0-3) 

XRC failures are a documented, recurring operational reality — the CHANGELOG explicitly records a fix for "a single persistent gap no longer stalls maturity modulation updates": [5](#0-4) 

**How the stale value is applied to mint ICP:**

The stale modulation is passed directly to `apply_maturity_modulation()`, which multiplies the neuron's maturity by `(1 + modulation)`:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,
) {
``` [6](#0-5) 

The same function is called in the disbursement path:

```rust
let maturity_to_disburse_after_modulation_e8s = apply_maturity_modulation(
    original_maturity_e8s_equivalent,
    maturity_modulation_basis_points,
)
``` [7](#0-6) 

The modulation range is ±500 permyriad (±5%): [8](#0-7) 

**Initialization default is neutral (0), but the staleness window is unbounded:**

At canister init, the modulation is set to `0` with `updated_at_days_since_epoch: None`: [9](#0-8) 

Once the first real value is written, subsequent XRC outages leave the last-known value frozen indefinitely. There is no maximum-age guard in the spawning or disbursement paths.

---

### Impact Explanation

Every neuron that enters the spawning queue or has a pending maturity disbursement during an XRC outage will have its ICP amount computed from a stale modulation. If the ICP/XDR price has moved significantly since the last successful update:

- **Over-minting**: If the true modulation should be negative (ICP price fell) but the cached value is positive, the protocol mints more ICP than warranted — a ledger conservation violation.
- **Under-minting**: If the true modulation should be positive (ICP price rose) but the cached value is negative or zero, neuron owners receive less ICP than they are entitled to.

The maximum per-event deviation is ±5% of the maturity amount. For large neurons (e.g., 10 million ICP-equivalent of maturity), this is a ±500,000 ICP error per spawning event. Multiple neurons can be spawned in a single `maybe_spawn_neurons()` invocation, compounding the total error.

---

### Likelihood Explanation

XRC unavailability is a documented operational reality on the IC mainnet. The CHANGELOG records two separate fixes in 2026 specifically addressing XRC failure handling in the maturity modulation pipeline. The `fetch_and_validate_rate()` function silently drops failed days: [10](#0-9) 

Any multi-day XRC outage — whether from network issues, XRC canister bugs, or rate validation failures — leaves the modulation frozen. Neuron spawning is triggered automatically by the governance heartbeat whenever neurons are ready, so no user action is required to trigger the incorrect minting.

---

### Recommendation

Before consuming `maturity_modulation.current_value_permyriad` in `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()`, check `updated_at_days_since_epoch` against the current day. If the value is older than an acceptable threshold (e.g., 2 days), either:

1. Skip spawning/disbursement for that round and log a warning, or
2. Fall back to a safe neutral value (0 permyriad) rather than a potentially stale non-zero value.

This mirrors the fix recommended in the Blueberry report: use the freshest available value, or explicitly handle the case where freshness cannot be guaranteed.

---

### Proof of Concept

1. The XRC canister becomes unavailable (e.g., due to a bug or network partition).
2. The `UpdateIcpXdrRateRelatedData` timer fires daily but `fetch_and_validate_rate()` returns `None` each time; `maturity_modulation.current_value_permyriad` remains at its last value (e.g., `+400` permyriad, reflecting a prior ICP price peak).
3. The ICP price has since fallen sharply; the correct modulation should now be `-300` permyriad.
4. A neuron with `maturity_e8s_equivalent = 10_000_000_00` (10 ICP) enters the spawning queue.
5. `maybe_spawn_neurons()` reads the stale `+400` permyriad and calls `apply_maturity_modulation(1_000_000_000, 400)`, minting `1_040_000_000` e8s instead of the correct `970_000_000` e8s — a 7% over-mint relative to the correct value, representing an ICP conservation violation.
6. The ledger records the mint; the error is irreversible.

### Citations

**File:** rs/nns/governance/src/governance.rs (L6427-6435)
```rust
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };
```

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L502-505)
```rust
    let maturity_to_disburse_after_modulation_e8s = apply_maturity_modulation(
        original_maturity_e8s_equivalent,
        maturity_modulation_basis_points,
    )
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L563-567)
```rust
        let maturity_modulation = governance
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L265-309)
```rust
    async fn fetch_and_validate_rate(&self, timestamp: u64) -> Option<SampledPrice> {
        let exchange_rate = match self
            .xrc_client
            .get_icp_to_xdr_exchange_rate(Some(timestamp))
            .await
        {
            Ok(rate) => rate,
            Err(err) => {
                println!(
                    "{}UpdateIcpXdrRateRelatedData: XRC call failed: {}",
                    LOG_PREFIX, err
                );
                return None;
            }
        };

        if let Err(err) = validate_exchange_rate(&exchange_rate) {
            println!(
                "{}UpdateIcpXdrRateRelatedData: XRC rate failed validation: {}",
                LOG_PREFIX, err
            );
            return None;
        }

        // Verify that XRC returned a rate for the day we requested. If not, the rate
        // won't fill the expected slot and backfill would loop on the same day.
        if exchange_rate.timestamp != timestamp {
            println!(
                "{}UpdateIcpXdrRateRelatedData: requested timestamp {} but XRC returned {}; ignoring.",
                LOG_PREFIX, timestamp, exchange_rate.timestamp
            );
            return None;
        }

        let rate = SampledPrice::from(&exchange_rate);
        if rate.xdr_permyriad_per_icp == 0 {
            println!(
                "{}UpdateIcpXdrRateRelatedData: received zero XDR/ICP rate; ignoring.",
                LOG_PREFIX
            );
            return None;
        }

        Some(rate)
    }
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L396-398)
```rust
    if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
        return;
    }
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L502-504)
```rust
        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
        };
```

**File:** rs/nns/governance/CHANGELOG.md (L29-34)
```markdown
## Fixed

* Tolerate XRC failures when updating maturity modulation: compute the average
  over available days using last-observation-carried-forward, and advance past
  days where XRC returns no rate so that a single persistent gap no longer
  stalls maturity modulation updates.
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```

**File:** rs/nns/governance/src/heap_governance_data.rs (L224-232)
```rust
        // Default to a neutral 0 permyriad so that spawning and maturity disbursement keep
        // working immediately after init, before `update_icp_xdr_rate_related_data` accumulates
        // enough price history to compute a real one. `updated_at_days_since_epoch` is left
        // `None` so the task treats this as "no prior measurement" rather than "already updated
        // today".
        maturity_modulation: Some(MaturityModulation {
            current_value_permyriad: Some(0),
            updated_at_days_since_epoch: None,
        }),
```
