### Title
Stale Maturity Modulation Used in ICP Minting Without Staleness Check - (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

`maybe_spawn_neurons()` and `finalize_maturity_disbursement` consume `heap_data.maturity_modulation.current_value_permyriad` to determine how much ICP to mint for neuron owners. Neither call site checks `updated_at_days_since_epoch` before using the cached value. If the `UpdateIcpXdrRateRelatedData` timer task fails to update the modulation for an extended period (e.g., persistent XRC unavailability), the stale modulation is silently applied to every spawn and disbursement, causing incorrect ICP amounts to be minted.

### Finding Description

The `MaturityModulation` protobuf message stores two fields: `current_value_permyriad` (the rate applied to maturity) and `updated_at_days_since_epoch` (when it was last computed). [1](#0-0) 

The daily timer task `UpdateIcpXdrRateRelatedData` fetches ICP/XDR rates from XRC and recomputes the modulation. When XRC fails, `update_maturity_modulation` explicitly preserves the prior value unchanged: [2](#0-1) 

The consuming code in `maybe_spawn_neurons()` reads `current_value_permyriad` directly with no staleness guard: [3](#0-2) 

Similarly, `next_maturity_disbursement_to_finalize` accepts `maturity_modulation_basis_points` and applies it without any age check: [4](#0-3) 

The `updated_at_days_since_epoch` field is stored in persistent state but is never consulted by either minting path.

### Impact Explanation

If XRC is unavailable for an extended period (days to weeks), the modulation value frozen at the last successful computation is applied to all spawns and disbursements. Because the modulation range is `[-1000, +200]` permyriad (−10 % to +2 %), the worst-case divergence between the stale and correct value is 12 percentage points. Concretely:

- If ICP price has fallen sharply since the last update, the modulation should be near −10 % but remains at a positive value, causing governance to mint up to 12 % more ICP per spawn/disbursement than the protocol intends — an inflationary over-mint.
- If ICP price has risen sharply, neuron owners receive less ICP than they should.

Every neuron owner who spawns or disburses maturity during the stale window is affected. The error accumulates across all concurrent spawns.

### Likelihood Explanation

The `UpdateIcpXdrRateRelatedData` task retries on XRC failure with a 60-second interval: [5](#0-4) 

However, the in-memory `last_attempted_day_in_round` cursor resets on every canister upgrade: [6](#0-5) 

A canister upgrade followed by a period of XRC unavailability (or a persistent XRC error for a specific day's timestamp) leaves `maturity_modulation` stale while spawning and disbursement continue unimpeded. The CHANGELOG confirms the system recently switched spawning and disbursement to consume this locally-computed value: [7](#0-6) 

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons()` and `finalize_maturity_disbursement`, check `updated_at_days_since_epoch` against the current day. If the modulation is older than a configurable threshold (e.g., 2–3 days), either block the operation or fall back to the neutral 0-permyriad value rather than silently applying a potentially stale rate. The field already exists in persistent state; it only needs to be read at the call sites.

### Proof of Concept

1. Governance canister is upgraded; `last_attempted_day_in_round` resets to `None`.
2. XRC becomes transiently unavailable for 7 days; every `fetch_and_validate_rate` call returns `None` and the timer retries without updating `icp_price_history` or `maturity_modulation`.
3. ICP price drops 15 % during those 7 days; the correct modulation would be −1000 permyriad (−10 %).
4. `maturity_modulation.current_value_permyriad` remains at its pre-outage value of, say, +150 permyriad (+1.5 %).
5. A neuron owner calls `manage_neuron` → `Spawn`; `maybe_spawn_neurons()` reads `current_value_permyriad = 150` without checking `updated_at_days_since_epoch`, and mints `maturity × 1.015` instead of `maturity × 0.90` — an 11.5 % over-mint per spawning neuron. [8](#0-7) [9](#0-8)

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L56-57)
```rust
/// Retry delay after a transient XRC failure. Short so we recover quickly without hammering XRC.
const ERROR_RETRY_INTERVAL_SECONDS: u64 = 60;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L202-207)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L497-503)
```rust
        let maybe_rate = self
            .fetch_and_validate_rate(day_to_fetch * ONE_DAY_SECONDS)
            .await;
        self.last_attempted_day_in_round = Some(day_to_fetch);

        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
```

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

**File:** rs/nns/governance/CHANGELOG.md (L20-22)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```
