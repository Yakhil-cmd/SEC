### Title
Stale XRC-Derived Maturity Modulation Consumed Without Freshness Check in Neuron Spawning — (File: rs/nns/governance/src/governance.rs)

### Summary
`maybe_spawn_neurons` reads `maturity_modulation.current_value_permyriad` to determine how much ICP to mint for each spawning neuron, but never checks `updated_at_days_since_epoch` to verify the value is fresh. The `MaturityModulation` struct explicitly carries a staleness indicator that is populated by the daily XRC-backed update task, yet the consumption site ignores it entirely. Additionally, the shared `validate_exchange_rate` helper used by both the CMC and Governance contains no staleness check on the rate's own timestamp.

### Finding Description

**Root cause 1 — `maybe_spawn_neurons` ignores `updated_at_days_since_epoch`**

In `rs/nns/governance/src/governance.rs`, `maybe_spawn_neurons` reads the maturity modulation value:

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

The `MaturityModulation` protobuf message explicitly carries `updated_at_days_since_epoch` to record when the value was last computed: [2](#0-1) 

`maybe_spawn_neurons` never reads `updated_at_days_since_epoch`. If the daily `UpdateIcpXdrRateRelatedData` task has been failing for days or weeks (XRC unavailable, canister upgrade gap, etc.), the last successfully computed permyriad value — which could be anywhere in `[-1000, +200]` — is silently used to mint ICP for every spawning neuron.

**Root cause 2 — `validate_exchange_rate` contains no timestamp staleness check**

The shared validation helper in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` only checks source counts:

```rust
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES { ... }
    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES { ... }
    Ok(())
}
``` [3](#0-2) 

There is no check that `exchange_rate.timestamp` is within an acceptable window of the caller's current time. This function is called by both the CMC's `update_exchange_rate`: [4](#0-3) 

and by Governance's `fetch_and_validate_rate`: [5](#0-4) 

The CMC's downstream `do_set_icp_xdr_conversion_rate` only enforces monotonically increasing timestamps (new > stored), not freshness relative to wall-clock time: [6](#0-5) 

### Impact Explanation

**Neuron spawning (Governance):** When `maybe_spawn_neurons` fires, it applies the stale permyriad value via `apply_maturity_modulation` to compute `neuron_stake` and then mints that amount of ICP to the ledger. The modulation range is `[-1000, +200]` permyriad (−10 % to +2 %). A stale value of +200 permyriad held for weeks while ICP price has dropped would cause every spawning neuron to receive 2 % more ICP than the current market warrants; a stale −1000 permyriad would cause 10 % less ICP to be minted. Because the ledger transfer is irreversible, the error cannot be corrected after the fact. [7](#0-6) 

**Cycles pricing (CMC):** A rate accepted by `validate_exchange_rate` with a timestamp many hours in the past (but still newer than the stored rate) would cause cycles to be priced on stale ICP/XDR data, potentially allowing users to mint cycles at a rate that no longer reflects market conditions.

### Likelihood Explanation

The NNS Governance CHANGELOG records that XRC failures have occurred in production and required explicit fixes:

> *"Tolerate XRC failures when updating maturity modulation: compute the average over available days using last-observation-carried-forward, and advance past days where XRC returns no rate so that a single persistent gap no longer stalls maturity modulation updates."* [8](#0-7) 

This confirms that multi-day XRC outages are a realistic scenario. During such an outage the daily task silently preserves the last computed permyriad value, and `maybe_spawn_neurons` will use it without any age check for the entire duration of the outage. Any neuron owner whose neuron reaches its `spawn_at_timestamp_seconds` during the outage is affected.

### Recommendation

1. **Add a staleness guard in `maybe_spawn_neurons`**: Before using `current_value_permyriad`, verify that `updated_at_days_since_epoch` is within an acceptable number of days of `now / ONE_DAY_SECONDS` (e.g., ≤ 2 days). If the value is too stale, log and return without spawning, or fall back to a neutral 0-permyriad value.

2. **Add a timestamp freshness check in `validate_exchange_rate`**: Accept a `current_time_seconds` parameter and reject any `ExchangeRate` whose `timestamp` is older than a configurable staleness threshold (e.g., 2 × the refresh interval).

3. **Add a zero-rate check in `validate_exchange_rate`**: Currently the zero check is duplicated in downstream callers (`do_set_icp_xdr_conversion_rate` and `fetch_and_validate_rate`). Centralising it in `validate_exchange_rate` prevents future callers from omitting it.

### Proof of Concept

1. XRC becomes unavailable for 10 days (e.g., due to a subnet issue or a persistent `Pending` error).
2. `UpdateIcpXdrRateRelatedData::execute` fails on every tick; `maturity_modulation.updated_at_days_since_epoch` remains at day D while the canister clock advances to day D+10.
3. A neuron owner whose neuron's `spawn_at_timestamp_seconds` falls within this window calls no special method — the heartbeat calls `maybe_spawn_neurons` automatically.
4. `maybe_spawn_neurons` reads `current_value_permyriad = 200` (the last computed value from day D, when ICP was above its long-term average) and calls `apply_maturity_modulation(original_maturity, 200)`.
5. The neuron receives 2 % more ICP than the current (now lower) market price warrants, and the ledger transfer is final.

The entry path requires only a standard neuron owner with a neuron in spawning state — no privileged role, no governance majority, no threshold attack. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6421-6447)
```rust
    pub async fn maybe_spawn_neurons(&mut self) {
        if !self.can_spawn_neurons() {
            return;
        }

        let now_seconds = self.env.now();
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };

        // Sanity check that the maturity modulation returned is within bounds.
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

**File:** rs/nns/governance/src/governance.rs (L6484-6502)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
                        Err(err) => {
                            // Do not retain the lock so that other Neuron operations can continue.
                            // This is safe as no changes to the neuron have been made to the neuron
                            // both internally to governance and externally in ledger.
                            println!(
                                "{}Could not apply modulation to {:?} for neuron {:?} due to {:?}, skipping",
                                LOG_PREFIX,
                                neuron.maturity_e8s_equivalent,
                                neuron.id(),
                                err
                            );
                            continue;
                        }
                    };
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L111-129)
```rust
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughIcpSources {
            received: exchange_rate.metadata.base_asset_num_received_rates,
            queried: exchange_rate.metadata.base_asset_num_queried_sources,
        });
    }

    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughCxdrSources {
            received: exchange_rate.metadata.quote_asset_num_received_rates,
            queried: exchange_rate.metadata.quote_asset_num_queried_sources,
        });
    }

    Ok(())
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L259-263)
```rust
        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L281-287)
```rust
        if let Err(err) = validate_exchange_rate(&exchange_rate) {
            println!(
                "{}UpdateIcpXdrRateRelatedData: XRC rate failed validation: {}",
                LOG_PREFIX, err
            );
            return None;
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

**File:** rs/nns/cmc/src/main.rs (L1022-1030)
```rust
    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }
```

**File:** rs/nns/governance/CHANGELOG.md (L25-34)
```markdown
# 2026-05-13: Proposal 141771

http://dashboard.internetcomputer.org/proposal/141771

## Fixed

* Tolerate XRC failures when updating maturity modulation: compute the average
  over available days using last-observation-carried-forward, and advance past
  days where XRC returns no rate so that a single persistent gap no longer
  stalls maturity modulation updates.
```
