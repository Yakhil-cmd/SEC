### Title
Maturity Modulation Squatting — Neuron Holders Can Time `DisburseMaturity` Calls to Exploit Predictable Daily Modulation Changes - (File: rs/nns/governance/src/governance/disburse_maturity.rs)

---

### Summary
The NNS governance system applies the maturity modulation at **finalization time** (7 days after initiation), not at initiation time. Since the maturity modulation is publicly queryable via `get_maturity_modulation` and changes predictably (daily, with a known speed limit of 30 permyriad/day, bounded between −1,000 and +200 permyriad), any neuron holder can observe the current modulation trend and time their `DisburseMaturity` calls to maximize ICP received. This is the IC analog of the M-03 "rewards squatting" pattern.

---

### Finding Description

`initiate_maturity_disbursement` locks the disbursement *amount* at call time but does **not** lock the maturity modulation. Seven days later, `finalize_maturity_disbursement` reads `governance.heap_data.maturity_modulation.current_value_permyriad` — whatever the modulation happens to be at that moment — and applies it via `apply_maturity_modulation`. [1](#0-0) 

The disbursement amount is locked at initiation: [2](#0-1) 

But the modulation is read live at finalization from the current governance state: [3](#0-2) 

The modulation is then applied to the original locked amount: [4](#0-3) 

The modulation is updated once per day by the `UpdateIcpXdrRateRelatedData` timer task with a hard speed limit: [5](#0-4) 

The speed limit means the modulation at finalization is bounded: if the current value is `M`, the value 7 days later lies in `[M − 210, M + 210]` (clamped to `[−1000, +200]`). [6](#0-5) 

The modulation is publicly readable by any caller via the `get_maturity_modulation` query endpoint: [7](#0-6) 

The `MaturityModulation` struct and its semantics are documented in the protobuf: [8](#0-7) 

A rational neuron holder can therefore:
1. Query `get_maturity_modulation` to read the current modulation and observe its multi-day trend.
2. When the modulation is near its maximum (+200 permyriad, i.e., +2%), initiate disbursement. The worst-case modulation at finalization is `200 − 7×30 = −10` permyriad — essentially neutral.
3. When the modulation is deeply negative (e.g., −1,000 permyriad, i.e., −10%), delay disbursement until the modulation recovers.
4. Repeat across multiple neurons or disbursement windows to systematically harvest ICP at favorable modulation values.

The `spawn_neuron` / `maybe_spawn_neurons` path has the same structure — it reads `heap_data.maturity_modulation.current_value_permyriad` at spawn time: [9](#0-8) 

---

### Impact Explanation

- Sophisticated neuron holders (large institutional stakers, automated bots) can systematically receive more ICP per unit of maturity than passive holders.
- The maximum spread between best-case (+200 permyriad) and worst-case (−1,000 permyriad) is **12%**, representing a meaningful economic advantage at scale.
- The maturity modulation mechanism is designed to stabilize ICP price by discouraging selling when ICP is below its long-term average: [10](#0-9) 
- Systematic timing of disbursements undermines this stabilizing intent: sophisticated actors sell into favorable modulation windows, concentrating selling pressure exactly when the protocol is trying to reduce it.
- This is a **ledger conservation / governance economic attack**: ICP is minted in excess of what the protocol intends for informed actors, at the expense of the protocol's price-stabilization goal.

---

### Likelihood Explanation

- The entry path requires **no privileged access**: any principal controlling a neuron with sufficient maturity can call `DisburseMaturity`.
- The maturity modulation is a public query endpoint — no special tooling is needed to monitor it.
- The daily speed limit makes the modulation highly predictable over a 7-day horizon, making the timing strategy straightforward to implement with a simple monitoring script.
- Large neuron holders and automated staking services have strong economic incentives to implement this strategy.
- The `get_maturity_modulation` endpoint is explicitly exposed in the Candid interface: [11](#0-10) 

---

### Recommendation

- **Lock the modulation at initiation time**: record `maturity_modulation_basis_points` in the `MaturityDisbursement` struct at `initiate_maturity_disbursement` time and use that stored value at finalization, rather than reading the live value. This mirrors the M-03 fix of removing the ability to change the reward token mid-cycle. [12](#0-11) 
- Alternatively, apply a time-weighted average of the modulation over the 7-day window to reduce the advantage of precise timing.
- Apply the same fix to the `spawn_neuron` / `maybe_spawn_neurons` path.

---

### Proof of Concept

1. Call `get_maturity_modulation` on the NNS governance canister. Observe `current_value_permyriad = 200` (maximum, +2%) and that the trend has been positive for several days.
2. Call `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, ... }` on a neuron with large maturity (e.g., 1,000 ICP equivalent).
3. `initiate_maturity_disbursement` records `finalize_disbursement_timestamp_seconds = now + 7 × 86400` and deducts the maturity from the neuron. [13](#0-12) 
4. Seven days later, `finalize_maturity_disbursement` reads `governance.heap_data.maturity_modulation.current_value_permyriad`. Given the speed limit of 30 permyriad/day, the value is at worst `200 − 7×30 = −10` permyriad.
5. `apply_maturity_modulation(1_000_ICP_e8s, −10)` mints ≈ 99.9% of the original maturity as ICP — far better than the −10% a user would receive if they had initiated during a trough. [14](#0-13) 
6. A passive holder who initiated disbursement when the modulation was −1,000 permyriad receives only 90% of their maturity as ICP, while the timing-aware holder receives ≈100% — a **~10% advantage** on the same maturity amount, with zero additional risk or cost.

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L36-37)
```rust
/// The delay in seconds between initiating a maturity disbursement and the actual disbursement.
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L268-314)
```rust
    let timestamp_of_disbursement_seconds = now_seconds;
    let finalize_disbursement_timestamp_seconds = now_seconds + DISBURSEMENT_DELAY_SECONDS;

    let (
        is_neuron_spawning,
        is_neuron_controlled_by_caller,
        num_disbursements,
        maturity_e8s_equivalent,
    ) = neuron_store
        .with_neuron(id, |neuron| {
            let is_neuron_spawning = neuron.state(now_seconds) == NeuronState::Spawning;
            let is_neuron_controlled_by_caller = neuron.is_controlled_by(caller);
            let num_disbursements = neuron.maturity_disbursements_in_progress().len();
            let maturity_e8s_equivalent = neuron.maturity_e8s_equivalent;
            (
                is_neuron_spawning,
                is_neuron_controlled_by_caller,
                num_disbursements,
                maturity_e8s_equivalent,
            )
        })
        .map_err(|_| InitiateMaturityDisbursementError::NeuronNotFound)?;

    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }

    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
    if !is_neuron_controlled_by_caller {
        return Err(InitiateMaturityDisbursementError::CallerIsNotNeuronController);
    }
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }

    let disbursement_in_progress = MaturityDisbursement {
        destination: Some(destination),
        amount_e8s: disbursement_maturity_e8s,
        timestamp_of_disbursement_seconds,
        finalize_disbursement_timestamp_seconds,
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L18-26)
```rust
// ---- Maturity modulation algorithm ----
//
// Maturity modulation is the conversion factor from maturity to ICP. It is designed to have a
// stabilizing effect on the price of ICP: when the recent ICP price is above its long-term
// average, modulation is positive (more ICP per maturity), encouraging selling pressure; when
// below, modulation is negative (less ICP per maturity), discouraging selling.
//
// The result is in permyriad. For example, if this returns `mm` and the maturity being converted
// is `r`, the ICP minted is `r * (1 + mm / 10_000)`.
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-50)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L160-188)
```rust
    let speed_limited = match previous {
        // First calculation: no baseline to smooth from, so jump straight to target.
        None => target_modulation,
        Some((previous_permyriad, previous_day)) => {
            // Limit day-to-day change.
            let days_elapsed = current_day.saturating_sub(previous_day);
            let max_change = if days_elapsed > 1 {
                // The timer missed one or more days — allow proportionally more change.
                println!(
                    "{}compute_maturity_modulation_permyriad: {} days elapsed since last update (current_day={}, previous_day={})",
                    LOG_PREFIX, days_elapsed, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD.saturating_mul(days_elapsed as i64)
            } else if days_elapsed == 1 {
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            } else {
                // days_elapsed == 0: either same day or current_day < previous_day (should not happen).
                // Allow at least one day of movement.
                println!(
                    "{}compute_maturity_modulation_permyriad: days_elapsed=0 (current_day={}, previous_day={}); treating as 1 day",
                    LOG_PREFIX, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            };
            target_modulation.clamp(
                previous_permyriad.saturating_sub(max_change) as i128,
                previous_permyriad.saturating_add(max_change) as i128,
            )
        }
```

**File:** rs/nns/governance/canister/canister.rs (L581-588)
```rust
/// Returns the current maturity modulation, as defined by Mission 70.
#[query]
fn get_maturity_modulation(
    _request: GetMaturityModulationRequest,
) -> GetMaturityModulationResponse {
    debug_log("get_maturity_modulation");
    with_governance(|governance| governance.get_maturity_modulation())
}
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L3178-3204)
```rust
/// The maturity modulation factor is applied when disbursing (unstaked) maturity to ICP.
///
/// When a neuron owner disburses maturity, the amount of ICP received is:
///    maturity * (1 + current_value_permyriad / 10_000)
///
/// This factor stabilizes ICP price: it is positive when ICP is above its long-term average
/// (encouraging selling pressure), and negative when below (discouraging selling).
///
/// This might be unpopulated, which indicates that no value is currently available.
#[derive(
    candid::CandidType,
    candid::Deserialize,
    serde::Serialize,
    comparable::Comparable,
    Clone,
    Copy,
    PartialEq,
    ::prost::Message,
)]
pub struct MaturityModulation {
    /// Current maturity modulation in permyriad (0.01% per unit).
    #[prost(int32, optional, tag = "1")]
    pub current_value_permyriad: ::core::option::Option<i32>,
    /// Day (days_since_epoch) when current_value_permyriad was last computed.
    #[prost(uint64, optional, tag = "2")]
    pub updated_at_days_since_epoch: ::core::option::Option<u64>,
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

**File:** rs/nns/governance/canister/governance.did (L337-346)
```text
type MaturityModulation = record {
  current_value_permyriad : opt int32;
  updated_at_timestamp_seconds : opt nat64;
};

type GetMaturityModulationRequest = record {};

type GetMaturityModulationResponse = record {
  maturity_modulation : opt MaturityModulation;
};
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2774-2789)
```text
message MaturityDisbursement {
  // The amount of maturity being disbursed in e8s.
  uint64 amount_e8s = 1;
  // The timestamp at which the maturity was disbursed.
  uint64 timestamp_of_disbursement_seconds = 2;

  // The timestamp at which the maturity disbursement should be finalized.
  uint64 finalize_disbursement_timestamp_seconds = 4;

  oneof destination {
    // The icrc1 account to disburse the maturity to.
    Account account_to_disburse_to = 3;
    // The account identifier to disburse the maturity to.
    ic_ledger.pb.v1.AccountIdentifier account_identifier_to_disburse_to = 5;
  }
}
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
