### Title
Maturity Disbursement Timing Attack: User Can Front-Run Modulation by Choosing Disbursement Timing - (`File: rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

The NNS governance `disburse_maturity` instruction allows a neuron controller to initiate a maturity disbursement at any time. The disbursement is queued with a fixed 7-day delay, after which the *current* maturity modulation (±10% in permyriad) is applied at finalization time. Because the user controls *when* they call `disburse_maturity`, they can observe the publicly queryable `get_maturity_modulation` endpoint and time their disbursement initiation so that the 7-day window expires when the modulation is favorable (positive), gaining up to 10% more ICP than the neutral case. Conversely, they can delay initiating when modulation is negative. This is an analog of the perpetuals front-running bug: the user adjusts their pending request based on observable market/oracle conditions before the system executes it.

### Finding Description

The NNS governance canister implements a two-phase maturity disbursement:

1. **Initiation** (`disburse_maturity` / `initiate_maturity_disbursement`): The neuron controller calls `manage_neuron` with `DisburseMaturity`. The amount is deducted from `maturity_e8s_equivalent` and a `MaturityDisbursement` record is appended to `maturity_disbursements_in_progress` with `finalize_disbursement_timestamp_seconds = now + 7 days`.

2. **Finalization** (`try_finalize_maturity_disbursement`): A timer task runs after the 7-day delay and applies the *current* `maturity_modulation.current_value_permyriad` to compute the actual ICP minted.

The maturity modulation is publicly readable via the `get_maturity_modulation` query endpoint and is updated daily by the `UpdateIcpXdrRateRelatedData` timer task. The modulation ranges from −1000 to +200 permyriad (−10% to +2%) under Mission 70 bounds.

The critical issue: **the user chooses when to initiate the disbursement**, and the modulation applied is the one at finalization time (7 days later), not at initiation time. Since the modulation is a slow-moving, publicly observable value (updated daily, speed-limited to 30 permyriad/day), a user can:

- Query `get_maturity_modulation` to observe the current value and its trend.
- Initiate disbursement when the modulation is trending upward, so that 7 days later it is at or near its maximum (+200 permyriad = +2%).
- Delay initiating when modulation is negative or trending downward.

This is structurally identical to the perpetuals front-running bug: a pending request (disbursement) can be timed/adjusted by the user based on observable oracle/market data before the system executes it.

Additionally, the SNS governance `disburse_maturity` (in `rs/sns/governance/src/governance.rs`) has the same pattern: the modulation applied at finalization is the one cached at execution time, not at initiation time, and the user controls initiation timing.

### Impact Explanation

A neuron controller with sufficient maturity can systematically extract up to 2% more ICP per disbursement by timing initiations to coincide with favorable modulation windows. For large neuron holders (e.g., neurons with millions of ICP-equivalent maturity), this represents a material financial advantage over users who disburse without timing. The modulation is designed to have a stabilizing effect on ICP price; systematic exploitation of timing undermines this mechanism. The impact is a **ledger conservation / governance economic integrity** issue: more ICP is minted than the protocol intends for informed actors relative to uninformed ones.

### Likelihood Explanation

The attack requires no privileged access. Any neuron controller can:
1. Call the public `get_maturity_modulation` query (no authentication required).
2. Observe the daily trend (speed limit is 30 permyriad/day, so the trajectory is predictable 7 days out within ±210 permyriad).
3. Time `manage_neuron { DisburseMaturity }` calls accordingly.

This is a low-effort, zero-cost strategy for any sophisticated neuron holder. The 7-day window is long enough that the modulation trajectory is partially predictable. Likelihood is **high** for large neuron holders who are economically motivated.

### Recommendation

Record the maturity modulation value at **initiation time** and store it in the `MaturityDisbursement` record. Apply the stored modulation at finalization rather than the current one. This eliminates the timing advantage: the user cannot benefit from waiting for a favorable modulation because the modulation is locked in at the moment they commit to the disbursement.

Alternatively, apply modulation at initiation time (compute the final ICP amount immediately and store it), removing the 7-day modulation uncertainty entirely.

### Proof of Concept

**Step 1 – Observe modulation trend:**
```
dfx canister --network ic call nns-governance get_maturity_modulation '()'
// Returns: current_value_permyriad = -50, updated_at = yesterday
// Trend: rising (was -80 two days ago)
```

**Step 2 – Predict 7-day-out modulation:**
Speed limit is 30 permyriad/day. If current = −50 and trending up, in 7 days the value could reach up to −50 + 7×30 = +160 permyriad (+1.6%).

**Step 3 – Initiate disbursement now:**
```
dfx canister --network ic call nns-governance manage_neuron '(record {
  id = opt record { id = NEURON_ID };
  command = opt variant { DisburseMaturity = record {
    percentage_to_disburse = 100;
    to_account = null;
    to_account_identifier = null;
  }}
})'
```

**Step 4 – Wait 7 days.** The timer task `try_finalize_maturity_disbursement` fires, reads `governance.heap_data.maturity_modulation.current_value_permyriad` (now +160), and mints `amount_e8s * (10_000 + 160) / 10_000` ICP — 1.6% more than a user who disbursed at a neutral modulation.

**Root cause lines:** [1](#0-0) 

The `finalize_disbursement_timestamp_seconds` is set 7 days out, but no modulation is captured at initiation. [2](#0-1) 

At finalization, the *current* modulation is applied — not the one at initiation time. [3](#0-2) 

`try_finalize_maturity_disbursement` reads `current_value_permyriad` from live governance state at execution time, not from the stored disbursement record. [4](#0-3) 

The modulation is publicly observable, updated daily, and bounded/speed-limited — making its 7-day trajectory partially predictable. [5](#0-4) 

`get_maturity_modulation` is a public query endpoint, confirming the attacker can read the current modulation without any authentication.

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L268-269)
```rust
    let timestamp_of_disbursement_seconds = now_seconds;
    let finalize_disbursement_timestamp_seconds = now_seconds + DISBURSEMENT_DELAY_SECONDS;
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L29-51)
```rust
/// tracks recent price movements.
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

```

**File:** rs/nns/governance/src/governance.rs (L8027-8035)
```rust
    /// Returns the current maturity modulation, as defined by Mission 70.
    pub fn get_maturity_modulation(&self) -> GetMaturityModulationResponse {
        GetMaturityModulationResponse {
            maturity_modulation: self
                .heap_data
                .maturity_modulation
                .map(ApiMaturityModulation::from),
        }
    }
```
