### Title
Stale Maturity Modulation Rate Used Without Freshness Check in Neuron Spawning and Maturity Disbursement - (File: rs/nns/governance/src/governance.rs, rs/nns/governance/src/governance/disburse_maturity.rs)

### Summary

The NNS Governance canister caches a daily ICP/XDR-derived maturity modulation value in `heap_data.maturity_modulation.current_value_permyriad`. Both `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()` consume this cached value to determine how much ICP to mint, but neither function checks the companion `updated_at_days_since_epoch` field to verify the value is fresh. If the `UpdateIcpXdrRateRelatedData` timer task fails to update the modulation for an extended period, the stale value is silently applied to all subsequent neuron spawns and maturity disbursements, causing incorrect ICP amounts to be minted.

### Finding Description

The `MaturityModulation` protobuf struct stores two fields:
- `current_value_permyriad` — the modulation value in permyriad (±500 max)
- `updated_at_days_since_epoch` — the day the value was last computed [1](#0-0) 

The `UpdateIcpXdrRateRelatedData` recurring timer task fetches ICP/XDR rates from the Exchange Rate Canister (XRC) daily and recomputes the modulation. If XRC calls fail persistently, `update_maturity_modulation` logs the failure and leaves the prior modulation value unchanged: [2](#0-1) 

When `maybe_spawn_neurons()` runs, it reads only `current_value_permyriad` and never inspects `updated_at_days_since_epoch`: [3](#0-2) 

The same pattern appears in `try_finalize_maturity_disbursement()`: [4](#0-3) 

The `updated_at_days_since_epoch` field is populated by the timer task but is never consulted at the point of consumption. There is no guard that prevents a value that is days or weeks old from being applied to ICP minting operations.

### Impact Explanation

The maturity modulation is applied multiplicatively to the ICP amount minted during neuron spawning and maturity disbursement via `apply_maturity_modulation`: [5](#0-4) 

A stale modulation value (up to ±500 basis points, i.e., ±5%) causes every neuron spawn and maturity disbursement to mint an incorrect amount of ICP. If the modulation is stuck at +500 bp, all disbursements mint 5% more ICP than the current price ratio warrants; if stuck at −500 bp, 5% less. This is a ledger conservation issue: the total ICP supply diverges from what the protocol intends based on current market conditions. The effect accumulates across all neurons that spawn or disburse maturity while the stale value persists.

### Likelihood Explanation

The `UpdateIcpXdrRateRelatedData` task depends on the Exchange Rate Canister (XRC) being reachable and returning valid rates. The CHANGELOG documents that XRC failures have occurred in practice and required explicit fixes: [6](#0-5) 

If XRC is unavailable for more than one day, the modulation value becomes stale. Because there is no staleness guard at the consumption site, the stale value is used silently for an unbounded duration. Any neuron holder (unprivileged ingress sender) who calls `spawn_neuron` or `disburse_maturity` during a period of XRC unavailability will have their maturity converted using the stale rate. The likelihood is moderate: XRC outages are not hypothetical (they have occurred), and the window of exposure is the entire duration of the outage.

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()`, check `updated_at_days_since_epoch` against the current day. Define a maximum acceptable staleness (e.g., 2–3 days). If the cached value is older than the threshold, either skip the operation (return early / defer) or fall back to a neutral modulation of 0. For example:

```rust
let maturity_modulation = {
    let mm = self.heap_data.maturity_modulation.as_ref();
    let current_day = now_seconds / ONE_DAY_SECONDS;
    let is_fresh = mm
        .and_then(|m| m.updated_at_days_since_epoch)
        .map(|d| current_day.saturating_sub(d) <= MAX_STALE_DAYS)
        .unwrap_or(false);
    if !is_fresh {
        // Log and skip or use neutral 0
        return;
    }
    match mm.and_then(|m| m.current_value_permyriad) {
        None => return,
        Some(v) => v,
    }
};
```

### Proof of Concept

1. The NNS Governance canister is deployed. The `UpdateIcpXdrRateRelatedData` timer task runs and sets `maturity_modulation.current_value_permyriad = 500` (maximum positive) and `updated_at_days_since_epoch = day_N`.
2. The XRC canister becomes unavailable. For the next several days, every call to `fetch_and_validate_rate` returns `None`, and `update_maturity_modulation` logs a skip, leaving `current_value_permyriad = 500` and `updated_at_days_since_epoch = day_N` unchanged.
3. On day N+7, a neuron holder calls `disburse_maturity` with 100 ICP worth of maturity. The `finalize_maturity_disbursement` timer fires, reads `current_value_permyriad = 500` without checking `updated_at_days_since_epoch`, and mints `100 * (1 + 500/10_000) = 105 ICP` — 5% more than the current market-derived rate warrants.
4. The code path that reads the value: [4](#0-3) 

5. The code path that applies it without any age check: [7](#0-6) 

6. The `updated_at_days_since_epoch` field that exists but is never consulted at consumption time: [8](#0-7)

### Citations

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L3197-3204)
```rust
pub struct MaturityModulation {
    /// Current maturity modulation in permyriad (0.01% per unit).
    #[prost(int32, optional, tag = "1")]
    pub current_value_permyriad: ::core::option::Option<i32>,
    /// Day (days_since_epoch) when current_value_permyriad was last computed.
    #[prost(uint64, optional, tag = "2")]
    pub updated_at_days_since_epoch: ::core::option::Option<u64>,
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L417-428)
```rust
        Err(reason) => {
            // Reaches this branch only when the buffer has no rate at or before any day in the
            // recent window (e.g., a fresh canister where every backfill fetch has failed so far,
            // or every fetched rate was zero). Log and leave the prior modulation untouched —
            // subsequent rounds will retry the missing days.
            println!(
                "{}update_maturity_modulation: skipping update: {}; leaving prior modulation \
                 unchanged",
                LOG_PREFIX, reason
            );
        }
    }
```

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L499-512)
```rust
    // Apply the maturity modulation to the disbursement amount. This should not fail unless
    // something else in the system is wrong, such as an insanely large amount of maturity or an
    // incorrect maturity modulation basis points.
    let maturity_to_disburse_after_modulation_e8s = apply_maturity_modulation(
        original_maturity_e8s_equivalent,
        maturity_modulation_basis_points,
    )
    .map_err(
        |reason| FinalizeMaturityDisbursementError::MaturityModulationFailure {
            maturity_before_modulation_e8s: original_maturity_e8s_equivalent,
            maturity_modulation_basis_points,
            reason,
        },
    )?;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L561-574)
```rust
    let (maturity_disbursement_finalization, now_seconds) = governance.with_borrow(|governance| {
        let now_seconds = governance.env.now();
        let maturity_modulation = governance
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad);
        let maturity_disbursement_finalization = next_maturity_disbursement_to_finalize(
            &governance.neuron_store,
            &governance.heap_data.in_flight_commands,
            maturity_modulation,
            now_seconds,
        );
        (maturity_disbursement_finalization, now_seconds)
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L11-29)
```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    let amount_e8s = u128::from(amount_maturity_e8s);

    let adjusted_maturity_modulation_basis_points = saturating_add_or_subtract_u128_i32(
        BASIS_POINTS_PER_UNITY,
        maturity_modulation_basis_points,
    );

    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;

    u64::try_from(modulated_amount_e8s).map_err(|err| err.to_string())
}
```

**File:** rs/nns/governance/CHANGELOG.md (L29-34)
```markdown
## Fixed

* Tolerate XRC failures when updating maturity modulation: compute the average
  over available days using last-observation-carried-forward, and advance past
  days where XRC returns no rate so that a single persistent gap no longer
  stalls maturity modulation updates.
```
