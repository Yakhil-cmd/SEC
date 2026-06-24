### Title
No Staleness Check on `maturity_modulation.current_value_permyriad` Before Use in Neuron Spawning and Maturity Disbursement — (`rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister's `maybe_spawn_neurons()` reads `maturity_modulation.current_value_permyriad` and applies it to mint ICP for neuron holders without ever checking `updated_at_days_since_epoch`. The `MaturityModulation` struct carries a freshness field precisely for this purpose, but it is never consulted at the point of use. If the Exchange Rate Canister (XRC) is unavailable for any sustained period, the stale modulation value is silently applied to every neuron spawn and maturity disbursement, causing incorrect ICP amounts to be minted.

---

### Finding Description

`MaturityModulation` stores two fields:

- `current_value_permyriad` — the modulation factor applied when converting maturity to ICP
- `updated_at_days_since_epoch` — the day the value was last computed from live XRC data [1](#0-0) 

The daily timer task `UpdateIcpXdrRateRelatedData` fetches ICP/XDR rates from the XRC canister, validates them, and recomputes the modulation. If the XRC is unavailable, the task logs a failure and leaves `current_value_permyriad` unchanged — the `updated_at_days_since_epoch` field is not advanced. [2](#0-1) 

In `maybe_spawn_neurons()`, the code reads `current_value_permyriad` and checks only that it is `Some(...)` and within the global bounds `[-1000, 200]`. There is **no check** on `updated_at_days_since_epoch`: [3](#0-2) 

The same pattern applies in `next_maturity_disbursement_to_finalize()`, which accepts `maturity_modulation_basis_points: Option<i32>` and only rejects `None`: [4](#0-3) 

The `validate_exchange_rate()` function — the only validation applied to XRC responses — checks only source counts (`base_asset_num_received_rates >= 4`, `quote_asset_num_received_rates >= 4`). It does not check whether the returned rate's timestamp is unreasonably old relative to the current time: [5](#0-4) 

On canister initialization, `maturity_modulation` is seeded with `current_value_permyriad: Some(0)` and `updated_at_days_since_epoch: None`, so spawning and disbursement proceed immediately with a neutral-but-unverified value: [6](#0-5) 

---

### Impact Explanation

The maturity modulation factor directly controls how much ICP is minted when a neuron owner spawns a neuron or finalizes a maturity disbursement:

> `ICP minted = maturity × (1 + current_value_permyriad / 10_000)` [7](#0-6) 

If the XRC canister is unavailable for N days, the modulation value is frozen at whatever it was last computed. The global bounds allow values up to ±10% (`[-1000, 200]` permyriad). A neuron holder with 10,000 ICP of maturity could receive up to 1,000 ICP more or less than the protocol intends. Because `maybe_spawn_neurons()` processes all ready-to-spawn neurons in a single heartbeat invocation, the error is applied to every pending spawn simultaneously. [8](#0-7) 

---

### Likelihood Explanation

The XRC canister is a live dependency polled by a recurring async timer task. Transient XRC failures (rate limiting, subnet congestion, canister upgrades) are explicitly anticipated by the codebase — the retry logic schedules a new attempt after 60 seconds on failure: [9](#0-8) 

A sustained XRC outage of even a few days is realistic. During that window, every neuron spawn and maturity disbursement silently uses the stale modulation. The governance canister emits no alert, does not block spawning, and does not fall back to a neutral value. The `updated_at_days_since_epoch` field is stored in persistent state and is visible to callers of `get_maturity_modulation`, but it is never consulted by the spawning or disbursement code paths. [10](#0-9) 

---

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons()` and `next_maturity_disbursement_to_finalize()`, check `updated_at_days_since_epoch` against the current day. If the value is older than a defined staleness threshold (e.g., 2 days), either:

1. Fall back to a neutral modulation of `0` permyriad, or
2. Defer spawning/disbursement until a fresh value is available.

Example guard in `maybe_spawn_neurons()`:

```rust
let maturity_modulation = match self.heap_data.maturity_modulation.as_ref() {
    Some(mm) => {
        let current_day = now_seconds / ONE_DAY_SECONDS;
        let updated_day = mm.updated_at_days_since_epoch.unwrap_or(0);
        if current_day.saturating_sub(updated_day) > MAX_STALE_DAYS {
            println!("{}Maturity modulation is stale (updated_day={}, current_day={}); skipping spawn.",
                LOG_PREFIX, updated_day, current_day);
            return;
        }
        match mm.current_value_permyriad {
            None => return,
            Some(v) => v,
        }
    }
    None => return,
};
```

Additionally, `validate_exchange_rate()` should be extended to reject rates whose `timestamp` is more than a configurable number of seconds in the past relative to the current canister time, analogous to the Chainlink `require(timeStamp != 0)` / `require(answeredInRound >= roundID)` checks. [11](#0-10) 

---

### Proof of Concept

1. The XRC canister becomes unavailable (e.g., subnet congestion, rate limiting, or a canister upgrade that takes longer than expected).
2. `UpdateIcpXdrRateRelatedData::execute()` calls `fetch_and_validate_rate()`, which calls `xrc_client.get_icp_to_xdr_exchange_rate(Some(timestamp))` and receives an error. The task logs the failure and returns after `ERROR_RETRY_INTERVAL_SECONDS` (60 s). [12](#0-11) 
3. After N days of XRC unavailability, `maturity_modulation.updated_at_days_since_epoch` is N days behind `current_day`, but `current_value_permyriad` retains its last computed value (e.g., `+150` permyriad = +1.5%).
4. A neuron whose `spawn_at_timestamp_seconds` has elapsed is picked up by `maybe_spawn_neurons()`. The code reads `current_value_permyriad = 150` without checking `updated_at_days_since_epoch`. [13](#0-12) 
5. `apply_maturity_modulation(original_maturity, 150)` mints 1.5% more ICP than the protocol would compute with a fresh modulation, for every neuron spawned during the outage window.
6. The same stale value is applied in `next_maturity_disbursement_to_finalize()` for all pending maturity disbursements. [14](#0-13)

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L56-57)
```rust
/// Retry delay after a transient XRC failure. Short so we recover quickly without hammering XRC.
const ERROR_RETRY_INTERVAL_SECONDS: u64 = 60;
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L497-503)
```rust
        let maybe_rate = self
            .fetch_and_validate_rate(day_to_fetch * ONE_DAY_SECONDS)
            .await;
        self.last_attempted_day_in_round = Some(day_to_fetch);

        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
```

**File:** rs/nns/governance/src/governance.rs (L6427-6447)
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L454-512)
```rust
    maturity_modulation_basis_points: Option<i32>,
    now_seconds: u64,
) -> Result<Option<MaturityDisbursementFinalization>, FinalizeMaturityDisbursementError> {
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;

    // Try to find the first neuron eligible for finalizing maturity disbursement, that is not
    // locked.
    let Some(neuron_id) = neuron_store
        .get_neuron_ids_ready_to_finalize_maturity_disbursement(now_seconds)
        .into_iter()
        .find(|neuron_id| !in_flight_commands.contains_key(&neuron_id.id))
    else {
        // If all neurons are locked, we don't need to finalize anything.
        return Ok(None);
    };
    // Either of the errors below indicates a bug in the maturity disbursement index.
    let maturity_disbursement_in_progress = neuron_store
        .with_neuron(&neuron_id, |neuron| {
            neuron.maturity_disbursements_in_progress().first().cloned()
        })
        .map_err(|_| FinalizeMaturityDisbursementError::NeuronNotFound(neuron_id))?
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityDisbursement(
            neuron_id,
        ))?;

    let MaturityDisbursement {
        amount_e8s: original_maturity_e8s_equivalent,
        destination,
        finalize_disbursement_timestamp_seconds,
        timestamp_of_disbursement_seconds: _,
    } = maturity_disbursement_in_progress;

    // Make sure the disbursement is ready to be finalized. Failure at this step probably means the
    // maturity disbursement index is wrong.
    if now_seconds < finalize_disbursement_timestamp_seconds {
        return Err(
            FinalizeMaturityDisbursementError::NotTimeToFinalizeMaturityDisbursement {
                neuron_id,
                finalize_disbursement_timestamp_seconds,
                now_seconds,
            },
        );
    }

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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L110-129)
```rust
/// Validates that an ICP/CXDR exchange rate has enough sources.
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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2079-2094)
```text
// The maturity modulation factor is applied when disbursing (unstaked) maturity to ICP.
//
// When a neuron owner disburses maturity, the amount of ICP received is:
//   maturity * (1 + current_value_permyriad / 10_000)
//
// This factor stabilizes ICP price: it is positive when ICP is above its long-term average
// (encouraging selling pressure), and negative when below (discouraging selling).
//
// This might be unpopulated, which indicates that no value is currently available.
message MaturityModulation {
  // Current maturity modulation in permyriad (0.01% per unit).
  optional int32 current_value_permyriad = 1;

  // Day (days_since_epoch) when current_value_permyriad was last computed.
  optional uint64 updated_at_days_since_epoch = 2;
}
```

**File:** rs/nns/governance/src/governance/tests/get_maturity_modulation.rs (L23-42)
```rust
#[test]
fn defaults_to_zero_at_init() {
    // `initialize_governance` seeds `heap_data.maturity_modulation` with a neutral 0-permyriad
    // value at init so spawning and disbursement keep working immediately rather than early-
    // returning while the XRC-fed price history task accumulates enough data to compute a real
    // value. `updated_at` is left absent until the task produces a real measurement.
    let governance = make_governance();

    let response = governance.get_maturity_modulation();

    assert_eq!(
        response,
        GetMaturityModulationResponse {
            maturity_modulation: Some(ApiMaturityModulation {
                current_value_permyriad: Some(0),
                updated_at_timestamp_seconds: None,
            }),
        }
    );
}
```
