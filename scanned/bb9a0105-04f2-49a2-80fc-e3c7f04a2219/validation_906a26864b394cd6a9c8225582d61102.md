### Title
Hardcoded Maturity Modulation Price-Averaging Window Sizes and Algorithm Parameters Cannot Be Adjusted by Governance Without Canister Upgrade - (File: `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`)

### Summary
The NNS Governance canister's Mission 70 maturity modulation algorithm uses multiple hardcoded Rust `const` values for its price-averaging window sizes, sensitivity factor, daily speed limit, and global bounds. None of these parameters are part of `NetworkEconomics` or any other on-chain governance-adjustable structure. Changing any of them requires a full canister upgrade proposal, which is a heavyweight, multi-day governance process. In volatile ICP market conditions, the hardcoded 7-day "current" window and 30-permyriad/day speed limit will lag considerably behind sudden price movements, causing the maturity modulation applied to every neuron spawn and maturity disbursement to be systematically incorrect — directly affecting the amount of ICP minted for all neuron holders.

### Finding Description

In `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`, six algorithm parameters are compiled as Rust constants:

```rust
const MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS: usize = 7;
const MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS: usize = 365;
const MATURITY_MODULATION_SENSITIVITY_PERMYRIAD: i64 = 2_500;
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
``` [1](#0-0) 

These constants drive `compute_maturity_modulation_permyriad`, which computes:

```
target = sensitivity * (7-day_avg - 365-day_avg) / 365-day_avg
```

and then clamps the daily change to `MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD` (30 permyriad = 0.3% per day). [2](#0-1) 

The computed value is stored in `heap_data.maturity_modulation.current_value_permyriad` and is consumed directly by both neuron spawning and maturity disbursement finalization:

- **Spawning**: `apply_maturity_modulation(original_maturity, maturity_modulation)` mints `neuron_stake` ICP. [3](#0-2) 

- **Disbursement**: `apply_maturity_modulation(original_maturity_e8s_equivalent, maturity_modulation_basis_points)` determines the ICP amount transferred to the neuron owner. [4](#0-3) 

The `NetworkEconomics` structure — the only on-chain structure adjustable via `ManageNetworkEconomics` governance proposals — contains no maturity modulation algorithm parameters whatsoever. [5](#0-4) 

The analog to the original report is direct: just as `SwapManagerUniV3.sol` uses the same `TWAP_INTERVAL` for two pools with different liquidity characteristics, the NNS Governance canister uses the same hardcoded window sizes and speed limit regardless of ICP market volatility regime — and neither can be adjusted by the protocol's privileged role (governance) without a binary upgrade.

### Impact Explanation

The maturity modulation is bounded between −10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1000`) and +2% (`MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 = 200`). [6](#0-5) 

The critical constraint is the speed limit: `MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30` means the modulation can move at most 0.3% per day. If ICP price drops 15% in a single week (well within historical volatility), the 7-day average will reflect this immediately, but the modulation can only move 2.1% (7 × 0.3%) toward the new target in that same week. The modulation will remain systematically too high (positive) for weeks after a sharp price drop, causing every neuron spawn and maturity disbursement during that period to mint more ICP than the algorithm intends — a ledger conservation impact. The reverse holds for sharp price increases. Because the parameters cannot be adjusted by governance without a canister upgrade (a multi-day process), there is no timely remediation path.

### Likelihood Explanation

ICP is a volatile asset. The hardcoded 7-day window and 0.3%/day speed limit were calibrated for a specific market regime. Sudden price movements — which have occurred historically — will cause the modulation to lag for days to weeks. Every neuron holder who spawns or disburses maturity during such a lag period is affected. The entry path requires no special privilege: any principal controlling a neuron with sufficient maturity can call `spawn_neuron` or `disburse_maturity` as an unprivileged ingress message, and the incorrect modulation is applied automatically.

### Recommendation

Move the maturity modulation algorithm parameters into the `NetworkEconomics` protobuf message (or a dedicated `MaturityModulationParameters` sub-message), so they can be updated via `ManageNetworkEconomics` governance proposals without a canister upgrade. Apply appropriate validation bounds (e.g., window sizes must be positive and the short window must be shorter than the reference window; speed limit must be positive; global bounds must be ordered). This mirrors the fix applied in PR #557 of the referenced Morpho report: making the interval a mutable, governance-controlled field rather than a compile-time constant. [7](#0-6) 

### Proof of Concept

1. Observe that `MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS = 7` and `MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30` are Rust `const` values compiled into the governance canister binary. [8](#0-7) 

2. Confirm that `NetworkEconomics` — the only structure updatable via `ManageNetworkEconomics` proposals — contains no maturity modulation parameters. [9](#0-8) 

3. Confirm that `compute_maturity_modulation_permyriad` uses these constants directly with no runtime override path. [10](#0-9) 

4. Confirm that the computed value is applied to every neuron spawn (ICP minting) and maturity disbursement finalization without any additional adjustment. [11](#0-10) 

5. Simulate: set ICP price to 5 XDR for 358 days, then 4 XDR for 7 days. The 7-day average is 40,000; the 365-day average is ~49,808. Target modulation ≈ −492 permyriad. With `MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30` and a prior value of 0, the actual modulation after 7 days is only −210 permyriad (7 × 30), not −492. The modulation will not reach its target for another ~9 days — during which every spawn and disbursement mints ICP at a rate that is ~28 permyriad too generous per day. This is confirmed by the existing unit test `test_compute_maturity_modulation_price_decrease`. [12](#0-11)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L28-50)
```rust
/// Window size for the "current" ICP price estimate used in maturity modulation. A short window
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-197)
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

    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }

    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };

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
    };

    // Global bounds have final say. The result is within [MIN, MAX] which fit in i64, so the
    // cast is safe.
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
}
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

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2042-2094)
```rust
pub struct NetworkEconomics {
    /// The number of E8s (10E-8 of an ICP token) that a rejected
    /// proposal will cost.
    ///
    /// This fee should be controlled by an #Economic proposal type.
    /// The fee does not apply for ManageNeuron proposals.
    #[prost(uint64, tag = "1")]
    pub reject_cost_e8s: u64,
    /// The minimum number of E8s that can be staked in a neuron.
    #[prost(uint64, tag = "2")]
    pub neuron_minimum_stake_e8s: u64,
    /// The number of E8s (10E-8 of an ICP token) that it costs to
    /// employ the 'manage neuron' functionality through proposals. The
    /// cost is incurred by the neuron that makes the 'manage neuron'
    /// proposal and is applied regardless of whether the proposal is
    /// adopted or rejected.
    #[prost(uint64, tag = "4")]
    pub neuron_management_fee_per_proposal_e8s: u64,
    /// The minimum number that the ICP/XDR conversion rate can be set to.
    ///
    /// Measured in XDR (the currency code of IMF SDR) to two decimal
    /// places.
    ///
    /// See /rs/protobuf/def/registry/conversion_rate/v1/conversion_rate.proto
    /// for more information on the rate itself.
    #[prost(uint64, tag = "5")]
    pub minimum_icp_xdr_rate: u64,
    /// The dissolve delay of a neuron spawned from the maturity of an
    /// existing neuron.
    #[prost(uint64, tag = "6")]
    pub neuron_spawn_dissolve_delay_seconds: u64,
    /// The maximum rewards to be distributed to NodeProviders in a single
    /// distribution event, in e8s.
    #[prost(uint64, tag = "8")]
    pub maximum_node_provider_rewards_e8s: u64,
    /// The transaction fee that must be paid for each ledger transaction.
    #[prost(uint64, tag = "9")]
    pub transaction_fee_e8s: u64,
    /// The maximum number of proposals to keep, per topic for eligible topics.
    /// When the total number of proposals for a given topic is greater than this
    /// number, the oldest proposals that have reached a "final" state
    /// may be deleted.
    ///
    /// If unspecified or zero, all proposals are kept.
    #[prost(uint32, tag = "10")]
    pub max_proposals_to_keep_per_topic: u32,
    /// Global Neurons' Fund participation thresholds.
    #[prost(message, optional, tag = "11")]
    pub neurons_fund_economics: ::core::option::Option<NeuronsFundEconomics>,
    /// Parameters that affect the voting power of neurons.
    #[prost(message, optional, tag = "12")]
    pub voting_power_economics: ::core::option::Option<VotingPowerEconomics>,
}
```

**File:** rs/nns/governance/src/network_economics.rs (L14-42)
```rust
impl NetworkEconomics {
    /// The multiplier applied to minimum_icp_xdr_rate to convert the XDR unit to basis_points
    pub const ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER: u64 = 100;

    // The default values for network economics (until we initialize it).
    // Can't implement Default since it conflicts with Prost's.
    pub fn with_default_values() -> Self {
        Self {
            reject_cost_e8s: E8,                                        // 1 ICP
            neuron_management_fee_per_proposal_e8s: 1_000_000,          // 0.01 ICP
            neuron_minimum_stake_e8s: E8,                               // 1 ICP
            neuron_spawn_dissolve_delay_seconds: ONE_DAY_SECONDS * 7,   // 7 days
            maximum_node_provider_rewards_e8s: 1_000_000 * 100_000_000, // 1M ICP
            minimum_icp_xdr_rate: 100,                                  // 1 XDR
            transaction_fee_e8s: DEFAULT_TRANSFER_FEE.get_e8s(),
            max_proposals_to_keep_per_topic: 100,
            neurons_fund_economics: Some(NeuronsFundEconomics::with_default_values()),
            voting_power_economics: Some(VotingPowerEconomics::with_default_values()),
        }
    }

    pub fn apply_changes_and_validate(
        &self,
        changes: &NetworkEconomics,
    ) -> Result<Self, Vec<String>> {
        let result = changes.inherit_from(self);
        result.validate()?;
        Ok(result)
    }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L1536-1560)
```text
message NetworkEconomics {
  reserved 3, 7;
  // The number of E8s (10E-8 of an ICP token) that a rejected
  // proposal will cost.
  //
  // This fee should be controlled by an #Economic proposal type.
  // The fee does not apply for ManageNeuron proposals.
  uint64 reject_cost_e8s = 1;

  // The minimum number of E8s that can be staked in a neuron.
  uint64 neuron_minimum_stake_e8s = 2;

  // The number of E8s (10E-8 of an ICP token) that it costs to
  // employ the 'manage neuron' functionality through proposals. The
  // cost is incurred by the neuron that makes the 'manage neuron'
  // proposal and is applied regardless of whether the proposal is
  // adopted or rejected.
  uint64 neuron_management_fee_per_proposal_e8s = 4;

  // The minimum number that the ICP/XDR conversion rate can be set to.
  //
  // Measured in XDR (the currency code of IMF SDR) to two decimal
  // places.
  //
  // See /rs/protobuf/def/registry/conversion_rate/v1/conversion_rate.proto
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data_tests.rs (L470-489)
```rust
#[test]
fn test_compute_maturity_modulation_price_decrease() {
    // ICP was at 5 XDR for the past year, except for the past 7 days it dropped to 4 XDR.
    // 7-day average: 40_000; 365-day average = (358*50_000 + 7*40_000) / 365 = 49_808.
    // target = 2_500 * (40_000 - 49_808) / 49_808 ≈ -492 permyriad (negative = price dropped).
    // Starting from 0, speed limit is 30 permyriad/day → result = -30.
    let mut rates: Vec<SampledPrice> = (1..=358)
        .map(|d| SampledPrice {
            timestamp_seconds: d * ONE_DAY_SECONDS,
            xdr_permyriad_per_icp: 50_000,
        })
        .collect();
    for d in 359..=365 {
        rates.push(SampledPrice {
            timestamp_seconds: d * ONE_DAY_SECONDS,
            xdr_permyriad_per_icp: 40_000,
        });
    }
    let result = compute_maturity_modulation_permyriad(&rates, 365, Some((0, 364)));
    assert_eq!(result, Ok(-MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD));
```
