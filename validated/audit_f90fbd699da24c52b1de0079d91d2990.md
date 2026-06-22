### Title
Stale Maturity Modulation Applied to ICP Minting Without Freshness Validation — (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

The NNS Governance canister (as of Proposal 141779) uses a locally computed `maturity_modulation.current_value_permyriad` — derived from XRC-backed ICP/XDR price history — to determine the ICP amount minted when neurons are spawned or maturity is disbursed. Neither `maybe_spawn_neurons` nor `try_finalize_maturity_disbursement` validates that `updated_at_days_since_epoch` is recent before applying the modulation. If the daily `UpdateIcpXdrRateRelatedData` timer task fails to refresh the value (e.g., due to persistent XRC unavailability), the stale modulation is silently applied to all subsequent minting operations indefinitely.

---

### Finding Description

`maybe_spawn_neurons` reads the maturity modulation as follows:

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

The only guard applied after this is a range check:

```rust
if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
    ...
    return;
}
``` [2](#0-1) 

There is no check on `updated_at_days_since_epoch`. The `MaturityModulation` struct carries both fields:

```rust
pub struct MaturityModulation {
    pub current_value_permyriad: ::core::option::Option<i32>,
    pub updated_at_days_since_epoch: ::core::option::Option<u64>,
}
``` [3](#0-2) 

The same pattern appears in `try_finalize_maturity_disbursement`, which reads `current_value_permyriad` without any freshness check before passing it to `next_maturity_disbursement_to_finalize`:

```rust
let maturity_modulation = governance
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad);
``` [4](#0-3) 

The value is refreshed by the `UpdateIcpXdrRateRelatedData` recurring timer task, which runs once per day and fetches ICP/XDR rates from the Exchange Rate Canister (XRC). If XRC is persistently unavailable or the timer fails to fire, `updated_at_days_since_epoch` falls arbitrarily far behind `now`, but `current_value_permyriad` is never cleared and continues to be used:

```rust
Err(reason) => {
    // ... Log and leave the prior modulation untouched —
    // subsequent rounds will retry the missing days.
    println!(...);
}
``` [5](#0-4) 

At canister initialization, the modulation is seeded with `current_value_permyriad: Some(0)` and `updated_at_days_since_epoch: None`, explicitly so that spawning and disbursement proceed immediately before the timer has ever run:

```rust
maturity_modulation: Some(MaturityModulation {
    current_value_permyriad: Some(0),
    updated_at_days_since_epoch: None,
}),
``` [6](#0-5) 

This means the code is intentionally designed to use whatever value is present, regardless of age.

---

### Impact Explanation

Any NNS governance user who calls `manage_neuron` to spawn a neuron or disburse maturity triggers `maybe_spawn_neurons` or `finalize_maturity_disbursement`, which applies `current_value_permyriad` to compute the ICP amount minted via `apply_maturity_modulation`. If the modulation is stale (e.g., weeks old), the ICP minted does not reflect the current ICP/XDR price relationship. The modulation range is bounded to `[-1000, +200]` permyriad (−10% to +2%), so the maximum per-disbursement error is 10% of the maturity amount. Over many disbursements during a period of XRC unavailability, this constitutes a systematic ledger conservation deviation — users receive incorrect ICP amounts relative to what the Mission 70 mechanism intends. [7](#0-6) 

---

### Likelihood Explanation

The `UpdateIcpXdrRateRelatedData` task is designed to be robust (LOCF gap-filling, cursor-advancing on failure), but persistent XRC unavailability lasting more than one day would leave the modulation stale. The XRC is an external canister dependency; if it returns errors for all days in a round, the modulation is not updated. The timer also resets its in-memory cursor on canister upgrade, meaning a sequence of upgrades during XRC downtime could extend the staleness window. The likelihood is low under normal conditions but non-negligible during XRC incidents. [8](#0-7) 

---

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons` and `try_finalize_maturity_disbursement`, validate that `updated_at_days_since_epoch` is within an acceptable staleness bound (e.g., no more than 2–3 days behind `now / ONE_DAY_SECONDS`). If the modulation is too stale, either skip the operation (returning early) or fall back to a neutral 0-permyriad value with a log warning. This mirrors the `_assertMinIntervalBetweenUpdatesPassed` pattern from the referenced RedStone report.

---

### Proof of Concept

1. Deploy NNS governance canister. The `UpdateIcpXdrRateRelatedData` timer sets `maturity_modulation` to, say, `+200` permyriad (the maximum) on day D.
2. XRC becomes persistently unavailable. The timer fires daily but every fetch fails; `current_value_permyriad` remains `+200`, `updated_at_days_since_epoch` remains `D`.
3. On day D+30, a neuron owner calls `manage_neuron { DisburseMaturity { percentage_to_disburse: 100 } }`.
4. `try_finalize_maturity_disbursement` reads `current_value_permyriad = Some(200)` with no staleness check, and mints `maturity * 1.02` ICP — 2% more than the neutral amount — despite the ICP price having potentially moved significantly in 30 days.
5. The `updated_at_days_since_epoch` field (30 days stale) is never consulted. [9](#0-8) [10](#0-9)

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

**File:** rs/nns/governance/src/governance.rs (L6438-6447)
```rust
        if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
            println!(
                "{}Maturity modulation (in basis points) out-of-bounds. Should be in range [{}, {}], actually is: {}",
                LOG_PREFIX,
                MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70,
                MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70,
                maturity_modulation
            );
            return;
        }
```

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L563-567)
```rust
        let maturity_modulation = governance
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L199-207)
```rust
pub(super) struct UpdateIcpXdrRateRelatedData {
    governance: &'static LocalKey<RefCell<Governance>>,
    xrc_client: Arc<dyn ExchangeRateCanisterClient>,
    /// Highest day attempted in the current backfill round. Failed fetches advance this so the
    /// next tick moves on to other missing days instead of looping on one that keeps failing.
    /// Reset to `None` at the end of a round (when maturity modulation is updated). The state is
    /// in-memory only and resets across canister upgrades; that just means the next round retries
    /// everything from scratch, which is what the next-midnight tick would do anyway.
    last_attempted_day_in_round: Option<u64>,
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

**File:** rs/nns/governance/src/heap_governance_data.rs (L229-232)
```rust
        maturity_modulation: Some(MaturityModulation {
            current_value_permyriad: Some(0),
            updated_at_days_since_epoch: None,
        }),
```
