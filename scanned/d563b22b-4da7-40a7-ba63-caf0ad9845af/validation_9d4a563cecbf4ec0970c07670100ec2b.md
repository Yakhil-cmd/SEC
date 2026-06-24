### Title
Stale Maturity-Modulation Rate Used Without Staleness Check Before Neuron Spawning and Maturity Disbursement - (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

The NNS Governance canister reads `heap_data.maturity_modulation.current_value_permyriad` directly when spawning neurons and finalizing maturity disbursements, without checking whether the cached value is stale. The `MaturityModulation` struct carries an `updated_at_days_since_epoch` field that signals when the value was last computed, but this staleness indicator is never consulted at the point of use. This is the IC analog of the FEI `isOutdated` inconsistency: the protocol stores a freshness timestamp alongside the oracle value but ignores it when the value is actually consumed for financial decisions.

---

### Finding Description

The NNS Governance canister maintains a locally-computed ICP/XDR price-based maturity modulation value in `heap_data.maturity_modulation` (type `MaturityModulation`), which has two fields:

- `current_value_permyriad: Option<i32>` — the modulation factor applied to maturity-to-ICP conversions
- `updated_at_days_since_epoch: Option<u64>` — the day on which the value was last computed

This value is updated by the `UpdateIcpXdrRateRelatedData` recurring timer task, which fetches daily ICP/XDR rates from the Exchange Rate Canister (XRC) and recomputes the modulation once per day.

**Consumption site 1 — `maybe_spawn_neurons`:** The function reads `current_value_permyriad` directly and applies it to mint ICP for all neurons ready to spawn. There is no check that `updated_at_days_since_epoch` is recent.

**Consumption site 2 — `try_finalize_maturity_disbursement` / `next_maturity_disbursement_to_finalize`:** The function reads `maturity_modulation.current_value_permyriad` and applies it to compute the ICP amount minted to the disbursement destination. Again, `updated_at_days_since_epoch` is never checked.

The `updated_at_days_since_epoch` field is only used inside the update path (`update_maturity_modulation`) to avoid recomputing on the same day. It is never consulted by the consumers.

Additionally, the NNS Governance canister seeds `maturity_modulation` at initialization with `current_value_permyriad = Some(0)` and `updated_at_days_since_epoch = None`, explicitly so that spawning and disbursement "keep working immediately" before the XRC-fed price history accumulates. This means the system is intentionally designed to use a value that has never been validated against real market data.

---

### Impact Explanation

The maturity modulation factor directly scales the ICP minted when a neuron spawns or when maturity is disbursed:

```
ICP minted = maturity * (1 + modulation / 10_000)
```

The modulation range is `[-1000, +200]` permyriad (i.e., −10% to +2%). If the cached value is stale (e.g., the XRC timer has been failing for days or weeks), the governance canister will continue minting ICP using an outdated modulation factor. In a scenario where the ICP price has dropped significantly but the cached modulation is still positive (from a prior high-price period), the protocol over-mints ICP relative to what the current price warrants. Conversely, a stale negative modulation during a price recovery under-mints ICP for neuron holders.

This affects every NNS neuron holder who spawns a neuron or finalizes a maturity disbursement during a period of XRC unavailability. The ICP ledger is directly mutated (minting), so the impact is a ledger conservation deviation — more or fewer ICP tokens are minted than the protocol intends.

---

### Likelihood Explanation

The XRC timer can fail silently for extended periods: `fetch_and_validate_rate` returns `None` on any XRC call failure, and the cursor simply advances. The `update_maturity_modulation` function explicitly documents that it "leaves prior modulation untouched" on failure. There is no circuit-breaker that blocks spawning or disbursement after N days without a fresh modulation value. The initialization path deliberately seeds a `0` modulation with `updated_at = None`, confirming the design accepts unbounded staleness at the consumption sites.

Any period of XRC unavailability (network partition, XRC canister upgrade, rate divergence triggering `Disabled` state in the CMC) will leave the modulation stale while spawning and disbursement continue unimpeded.

---

### Recommendation

Before applying `maturity_modulation` in `maybe_spawn_neurons` and `try_finalize_maturity_disbursement`, check that `updated_at_days_since_epoch` is within an acceptable window (e.g., ≤ 2 days old). If the value is stale, either:

1. Block the operation and return early (conservative), or
2. Fall back to a neutral `0` modulation with a log warning (permissive but bounded).

The staleness threshold should be consistent across both consumption sites. The `updated_at_days_since_epoch` field already exists for exactly this purpose and should be used.

---

### Proof of Concept

**Step 1.** The `MaturityModulation` struct stores a freshness indicator: [1](#0-0) 

**Step 2.** At initialization, `current_value_permyriad = Some(0)` is seeded with `updated_at_days_since_epoch = None`, explicitly so that spawning works before any real rate is fetched: [2](#0-1) 

**Step 3.** `maybe_spawn_neurons` reads `current_value_permyriad` with no staleness check on `updated_at_days_since_epoch`: [3](#0-2) 

**Step 4.** `try_finalize_maturity_disbursement` similarly reads `current_value_permyriad` with no staleness check: [4](#0-3) 

**Step 5.** `next_maturity_disbursement_to_finalize` applies the modulation directly to compute the ICP amount to mint: [5](#0-4) 

**Step 6.** The `update_maturity_modulation` function explicitly documents that on XRC failure it leaves the prior (potentially stale) value untouched: [6](#0-5) 

**Step 7.** The `updated_at_days_since_epoch` field is only used inside the update path to skip same-day recomputation — never at the consumption sites: [7](#0-6) 

**Step 8.** The modulation range is bounded at ±10%/+2%, meaning a stale value can cause up to 10% over- or under-minting of ICP relative to the intended protocol behavior: [8](#0-7)

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L396-398)
```rust
    if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
        return;
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
