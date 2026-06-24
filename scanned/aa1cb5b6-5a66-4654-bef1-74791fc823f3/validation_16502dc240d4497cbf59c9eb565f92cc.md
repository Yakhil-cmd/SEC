### Title
NNS Governance Maturity Disbursement and Neuron Spawning Universally Blocked When XRC-Derived Maturity Modulation Is Absent - (File: `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

The NNS governance canister's `finalize_maturity_disbursement` timer task and `maybe_spawn_neurons` function both unconditionally require `heap_data.maturity_modulation.current_value_permyriad` to be `Some(i32)`. This value is derived exclusively from the Exchange Rate Canister (XRC) via the `UpdateIcpXdrRateRelatedData` recurring timer task. When the XRC-backed price history is absent or insufficient — most critically on any fresh or reset governance canister — the field remains `None`, and every pending maturity disbursement and every neuron in spawning state is frozen for all NNS users simultaneously, with no per-user bypass or administrative override.

---

### Finding Description

**Proposal 141779 (2026-05-17)** switched neuron spawning and maturity disbursement finalization from reading the CMC-polled `cached_daily_maturity_modulation_basis_points` to reading a locally computed `maturity_modulation` field populated by the new `UpdateIcpXdrRateRelatedData` timer task. [1](#0-0) 

The timer task backfills up to 365 days of XRC price history at 5-second intervals (~30 minutes for a full fill), then computes the modulation only after the 7-day recent window contains at least one data point. [2](#0-1) 

On a fresh or reset governance canister, `heap_data.maturity_modulation` starts as `None`. The `update_maturity_modulation` helper preserves the prior value on any computation error — but if the prior value is `None`, it stays `None`. [3](#0-2) 

**Path 1 — Maturity disbursement finalization.** `try_finalize_maturity_disbursement` reads `maturity_modulation.current_value_permyriad` and passes it to `next_maturity_disbursement_to_finalize`: [4](#0-3) 

Inside that function, the very first statement hard-fails if the value is absent:

```rust
let maturity_modulation_basis_points = maturity_modulation_basis_points
    .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;
``` [5](#0-4) 

The outer `finalize_maturity_disbursement` logs the error and schedules a retry after `RETRY_INTERVAL`, but the retry will produce the same result as long as the modulation remains `None`. Every neuron whose disbursement window has elapsed is stuck. [6](#0-5) 

**Path 2 — Neuron spawning.** `maybe_spawn_neurons` silently returns without minting any spawning neuron when the field is absent:

```rust
let maturity_modulation = match self.heap_data.maturity_modulation
    .as_ref().and_then(|m| m.current_value_permyriad)
{
    None => return,
    Some(value) => value,
};
``` [7](#0-6) 

Neurons already placed in spawning state (maturity zeroed, `spawn_at_timestamp_seconds` set) remain frozen indefinitely.

The existing test suite explicitly confirms the `NoMaturityModulation` failure path: [8](#0-7) 

---

### Impact Explanation

**Scope — all NNS users simultaneously.** There is no per-neuron or per-user bypass. A single missing field in global governance state blocks every pending maturity disbursement and every spawning neuron across the entire NNS.

**Fund lock-up.** A user who calls `disburse_maturity` has their maturity immediately deducted from the neuron. If finalization is blocked, the maturity is gone from the neuron but the ICP is never minted. The user's funds are effectively frozen for an indefinite period.

**Spawning neuron lock-up.** A neuron placed in spawning state has its maturity zeroed and its stake set to the expected post-modulation amount. If `maybe_spawn_neurons` never runs successfully, the neuron remains in spawning state with no way for the owner to dissolve or otherwise manage it.

**No administrative escape hatch.** Neither function exposes a governance proposal or privileged call that could force finalization with a default modulation value. The only recovery path is waiting for the XRC backfill to complete and the timer to succeed.

---

### Likelihood Explanation

**Guaranteed on every fresh or reset deployment.** The `UpdateIcpXdrRateRelatedData` task requires at least one successful XRC fetch for a day within the 7-day recent window before `compute_maturity_modulation_permyriad` can return `Ok`. At 5-second intervals the backfill takes ~30 minutes to attempt all 365 days, but the modulation is only computed after the round completes. Any user who initiates a disbursement during this window and whose 7-day finalization deadline falls before the modulation is populated will be affected.

**Triggered by XRC errors during the critical window.** `fetch_and_validate_rate` returns `None` on any XRC call failure (network error, `Pending`, `RateLimited`, `StablecoinRateTooFewRates`, timestamp mismatch, zero rate, etc.). [9](#0-8) 

If every fetch in the 7-day recent window fails, `compute_maturity_modulation_permyriad` returns `Err("no rate available for the recent price window")` and the prior `None` is preserved. [10](#0-9) 

**Introduced by a recent production change.** Before Proposal 141779 the CMC-polled value was always available (the CMC initializes with a hardcoded default rate). The new local computation has no such default, making the `None` state reachable in production for the first time.

---

### Recommendation

1. **Provide a safe default.** Initialize `maturity_modulation.current_value_permyriad` to `Some(0)` (neutral, no modulation) rather than `None`. This matches the pre-Mission-70 behavior and unblocks disbursements while the XRC history is being built.

2. **Decouple finalization from modulation availability.** When `current_value_permyriad` is `None`, `finalize_maturity_disbursement` should apply zero modulation (1:1 maturity-to-ICP) rather than returning an error, consistent with the SNS governance's `maturity_modulation_disabled` path. [11](#0-10) 

3. **Retain the CMC-polled value as a fallback.** The old `cached_daily_maturity_modulation_basis_points` field could serve as a fallback when the XRC-derived value is absent, preventing a regression in availability.

---

### Proof of Concept

```
1. NNS governance canister is freshly deployed (or upgraded with state reset).
   → heap_data.maturity_modulation = None

2. User A calls manage_neuron { DisburseMaturity { percentage_to_disburse: 100 } }
   → Succeeds: maturity deducted from neuron, MaturityDisbursement queued with
     finalize_disbursement_timestamp_seconds = now + 7 days.

3. UpdateIcpXdrRateRelatedData timer fires every 5 s, backfilling XRC history.
   Suppose the XRC returns errors for all days in the 7-day recent window
   (e.g., StablecoinRateTooFewRates or Pending).
   → fetch_and_validate_rate returns None for each day.
   → compute_maturity_modulation_permyriad returns Err("no rate available…").
   → update_maturity_modulation preserves prior value: None.
   → heap_data.maturity_modulation.current_value_permyriad remains None.

4. Seven days pass. finalize_maturity_disbursement timer fires.
   try_finalize_maturity_disbursement reads current_value_permyriad = None.
   next_maturity_disbursement_to_finalize returns
     Err(FinalizeMaturityDisbursementError::NoMaturityModulation).
   → ICP is never minted. User A's maturity is permanently deducted with no payout.
   → Timer retries after RETRY_INTERVAL; same result indefinitely.

5. User B had called spawn_neuron earlier; their neuron is in spawning state.
   maybe_spawn_neurons reads current_value_permyriad = None → returns immediately.
   → Spawning neuron remains frozen; User B cannot dissolve or manage it.

All NNS users with pending disbursements or spawning neurons are affected
simultaneously by a single missing field in global governance state.
``` [12](#0-11) [7](#0-6) [13](#0-12)

### Citations

**File:** rs/nns/governance/CHANGELOG.md (L20-23)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.

```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L30-57)
```rust
const MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS: usize = 7;

/// Window size for the "reference" (long-term average) ICP price used in maturity modulation.
const MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS: usize = 365;

/// The sorted rate vector must hold enough days for the longest averaging window.
const MAX_RATES_BUFFER_SIZE: usize = MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS;

/// How much the relative difference between current and reference ICP price affects maturity
/// modulation. k = 0.25 means a 10% price increase yields a 2.5% modulation boost.
/// Expressed in permyriad: 0.25 * 10_000 = 2_500.
const MATURITY_MODULATION_SENSITIVITY_PERMYRIAD: i64 = 2_500;

/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;

/// Delay between consecutive XRC calls while backfilling historical rates. At 5 seconds per call,
/// filling the full 365-day window takes about 30 minutes.
const BACKFILL_INTERVAL_SECONDS: u64 = 5;

/// Retry delay after a transient XRC failure. Short so we recover quickly without hammering XRC.
const ERROR_RETRY_INTERVAL_SECONDS: u64 = 60;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-148)
```rust
fn compute_maturity_modulation_permyriad(
    rates: &[SampledPrice],
    current_day: u64,
    previous: Option<(i64, u64)>,
) -> Result<i64, String> {
    let recent_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the recent price window".to_string())?;

    let reference_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the reference price window".to_string())?;

```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L263-309)
```rust
    /// Fetches the ICP/XDR rate from XRC for `timestamp`, validates, and converts.
    /// Returns `None` if any step fails (errors are logged).
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L386-429)
```rust
/// Recomputes maturity modulation from the current price history and updates `maturity_modulation`.
///
/// Tolerates gaps in the price history: averages use LOCF in `compute_average_icp_xdr_rate`. If
/// the buffer has no rate at or before any day in the recent window, the calculation returns
/// `Err` and the prior modulation value is preserved.
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L451-458)
```rust
fn next_maturity_disbursement_to_finalize(
    neuron_store: &NeuronStore,
    in_flight_commands: &HashMap<u64, NeuronInFlightCommand>,
    maturity_modulation_basis_points: Option<i32>,
    now_seconds: u64,
) -> Result<Option<MaturityDisbursementFinalization>, FinalizeMaturityDisbursementError> {
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L544-554)
```rust
pub async fn finalize_maturity_disbursement(
    governance: &'static LocalKey<RefCell<Governance>>,
) -> Duration {
    match try_finalize_maturity_disbursement(governance).await {
        Ok(_) => governance.with_borrow(get_delay_until_next_finalization),
        Err(err) => {
            println!("FinalizeMaturityDisbursementTask failed: {}", err);
            RETRY_INTERVAL
        }
    }
}
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L561-575)
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
    });
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

**File:** rs/nns/governance/src/governance/disburse_maturity_tests.rs (L612-651)
```rust
#[tokio::test]
async fn test_finalize_maturity_disbursement_no_maturity_modulation() {
    // Step 1: Set up the test environment without maturity modulation.
    set_governance_for_test(
        vec![create_neuron_builder().build()],
        MockIcpLedger::default(),
        DEFAULT_MATURITY_MODULATION_BASIS_POINTS,
    );
    TEST_GOVERNANCE.with_borrow_mut(|governance| {
        governance.heap_data.maturity_modulation = None;
    });

    // Step 2: Initiate the maturity disbursement and advance to disbursement time.
    assert_eq!(
        TEST_GOVERNANCE.with_borrow_mut(|governance| {
            initiate_maturity_disbursement(
                &mut governance.neuron_store,
                &CONTROLLER,
                &NeuronId { id: 1 },
                &DisburseMaturity {
                    percentage_to_disburse: 1,
                    to_account: None,
                    to_account_identifier: None,
                },
                NOW_SECONDS,
            )
        }),
        Ok(1_000_000_000)
    );
    advance_time(DISBURSEMENT_DELAY_SECONDS);

    // Step 4: Finalize the maturity disbursement and verify that it fails.
    let result = try_finalize_maturity_disbursement(&TEST_GOVERNANCE)
        .now_or_never()
        .unwrap();
    assert_eq!(
        result,
        Err(FinalizeMaturityDisbursementError::NoMaturityModulation)
    );
}
```

**File:** rs/sns/governance/src/governance.rs (L402-428)
```rust
    fn effective_maturity_modulation_basis_points(&self) -> Result<i32, GovernanceError> {
        let maturity_modulation_disabled = self
            .parameters
            .as_ref()
            .map(|nervous_system_parameters| {
                nervous_system_parameters
                    .maturity_modulation_disabled
                    .unwrap_or_default()
            })
            .unwrap_or_default();

        if maturity_modulation_disabled {
            return Ok(0);
        }

        self.maturity_modulation
            .as_ref()
            .and_then(|maturity_modulation| maturity_modulation.current_basis_points)
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::Unavailable,
                    "Maturity modulation not known. Retrying later might work. \
                     If this persists, there is probably a problem with retrieving \
                     the maturity modulation value from the Cycles Minting Canister.",
                )
            })
    }
```
