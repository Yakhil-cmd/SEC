### Title
Stale Maturity Modulation Used in ICP Minting Without Staleness Check - (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary
`maybe_spawn_neurons` and `try_finalize_maturity_disbursement` in NNS Governance read `heap_data.maturity_modulation.current_value_permyriad` to determine how much ICP to mint, but neither function checks `updated_at_days_since_epoch` to verify the value is current. When the Exchange Rate Canister (XRC) is unavailable for one or more days, the prior modulation value is explicitly preserved stale and silently used for all subsequent ICP minting operations.

### Finding Description

The `MaturityModulation` struct stores two fields: `current_value_permyriad` (the rate) and `updated_at_days_since_epoch` (when it was last computed). [1](#0-0) 

The daily timer task `UpdateIcpXdrRateRelatedData` is responsible for refreshing this value. When XRC fails, `update_maturity_modulation` explicitly preserves the prior value unchanged: [2](#0-1) 

`maybe_spawn_neurons` reads `current_value_permyriad` directly with no staleness check against `updated_at_days_since_epoch`: [3](#0-2) 

`try_finalize_maturity_disbursement` does the same: [4](#0-3) 

The stale modulation value is then passed directly to `apply_maturity_modulation` to compute the ICP amount to mint: [5](#0-4) [6](#0-5) 

Additionally, on canister initialization, `maturity_modulation` is seeded with `current_value_permyriad: Some(0)` and `updated_at_days_since_epoch: None`, and is immediately usable for spawning/disbursement before any real XRC data is collected: [7](#0-6) 

### Impact Explanation

The maturity modulation is a price-sensitive factor applied to ICP minting: `minted_ICP = maturity * (1 + modulation/10_000)`. The modulation range is `[-500, +500]` permyriad (±5%), meaning a stale value can cause up to 10% deviation in the ICP amount minted relative to the correct current-day value.

When XRC is unavailable for N days and ICP price moves significantly during that window, all neuron spawning and maturity disbursement finalization during that window uses the stale modulation. This is a **ledger conservation bug**: the wrong quantity of ICP is minted. Depending on price direction, users receive either more or fewer ICP than the protocol intends, breaking the economic stabilization mechanism that maturity modulation is designed to enforce.

### Likelihood Explanation

XRC unavailability is a realistic and documented failure mode — the error handling code in `update_maturity_modulation` and `fetch_and_validate_rate` explicitly handles XRC call failures, and the CHANGELOG records a fix for "a single persistent gap no longer stalls maturity modulation updates." [8](#0-7) 

Any neuron holder can initiate a `spawn_neuron` or `DisburseMaturity` call, scheduling a future minting event. If XRC is down when that event finalizes (7 days later for disbursement), the stale modulation is used. A sophisticated actor can time disbursement initiation to ensure finalization falls during a known XRC outage window, or simply benefit opportunistically when ICP price has moved favorably relative to the stale modulation.

### Recommendation

Before using `current_value_permyriad` in `maybe_spawn_neurons` and `try_finalize_maturity_disbursement`, check `updated_at_days_since_epoch` against the current day. If the value is stale beyond an acceptable threshold (e.g., more than N days old), either abort the minting operation or apply a conservative fallback (e.g., 0 permyriad). The `updated_at_days_since_epoch` field already exists for exactly this purpose but is never consulted by consumers.

### Proof of Concept

1. Governance canister is running normally; `maturity_modulation.current_value_permyriad = +400` (ICP price was high), `updated_at_days_since_epoch = day D`.
2. XRC canister becomes unavailable starting day D+1. `update_maturity_modulation` fires daily but preserves the stale `+400` value each time.
3. ICP price drops sharply. The correct modulation for day D+5 would be `-300`.
4. A neuron holder whose disbursement finalizes on day D+5 calls `finalize_maturity_disbursement`. The function reads `current_value_permyriad = +400` (stale) without checking `updated_at_days_since_epoch`.
5. `apply_maturity_modulation(maturity_e8s, 400)` mints 4% more ICP than the current price warrants, instead of minting 3% less. The net error is 7% excess ICP minted per disbursement.
6. This repeats for every neuron spawning and disbursement finalization until XRC recovers and the modulation is refreshed. [9](#0-8) [10](#0-9)

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L391-429)
```rust
fn update_maturity_modulation(
    icp_price_history: &IcpPriceHistory,
    maturity_modulation: &mut MaturityModulation,
    current_day: u64,
) {
    if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
        return;
    }

    let previous = match (
        maturity_modulation.current_value_permyriad,
        maturity_modulation.updated_at_days_since_epoch,
    ) {
        (Some(p), Some(d)) => Some((p as i64, d)),
        _ => None,
    };

    match compute_maturity_modulation_permyriad(
        &icp_price_history.icp_xdr_rates,
        current_day,
        previous,
    ) {
        Ok(new_permyriad) => {
            maturity_modulation.current_value_permyriad = Some(new_permyriad as i32);
            maturity_modulation.updated_at_days_since_epoch = Some(current_day);
        }
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

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L502-512)
```rust
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L558-575)
```rust
async fn try_finalize_maturity_disbursement(
    governance: &'static LocalKey<RefCell<Governance>>,
) -> Result<(), FinalizeMaturityDisbursementError> {
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
    });
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

**File:** rs/nns/governance/CHANGELOG.md (L29-34)
```markdown
## Fixed

* Tolerate XRC failures when updating maturity modulation: compute the average
  over available days using last-observation-carried-forward, and advance past
  days where XRC returns no rate so that a single persistent gap no longer
  stalls maturity modulation updates.
```
