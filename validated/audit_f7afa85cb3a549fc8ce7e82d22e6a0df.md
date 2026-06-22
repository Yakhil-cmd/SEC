### Title
Zero or Unavailable ICP/XDR Oracle Rate Permanently Blocks Neuron Spawning and Maturity Disbursement — (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

When the Exchange Rate Canister (XRC) returns a zero ICP/XDR rate or is persistently unavailable, `maturity_modulation.current_value_permyriad` remains `None`. Both `maybe_spawn_neurons` and `finalize_maturity_disbursement` hard-gate on this field being `Some`, causing neurons already placed in `Spawning` state and all pending maturity disbursements to be blocked indefinitely — an exact structural analog to the GMX oracle-zero liquidation freeze.

---

### Finding Description

**Step 1 — Zero rate is rejected at the XRC client layer.**

`fetch_and_validate_rate` in `update_icp_xdr_rate_related_data.rs` explicitly discards any rate that converts to zero permyriad:

```rust
if rate.xdr_permyriad_per_icp == 0 {
    println!("...received zero XDR/ICP rate; ignoring.");
    return None;
}
``` [1](#0-0) 

This mirrors the GMX `EmptyFeedPrice` revert exactly.

**Step 2 — Zero average reference price also aborts modulation computation.**

`compute_maturity_modulation_permyriad` returns `Err` when the 365-day average ICP price is zero (e.g., if every stored rate is zero):

```rust
if reference_icp_price == 0 {
    return Err("reference price averaged to zero".to_string());
}
``` [2](#0-1) 

**Step 3 — `update_maturity_modulation` silently skips the update, leaving `current_value_permyriad` as `None`.**

On a fresh canister (or one where every XRC fetch has failed), `current_value_permyriad` is never populated. The error path only logs and preserves the prior value — but there is no prior value on a fresh canister:

```rust
Err(reason) => {
    println!("...skipping update: {}; leaving prior modulation unchanged", reason);
}
``` [3](#0-2) 

**Step 4 — `maybe_spawn_neurons` returns early when `current_value_permyriad` is `None`.**

```rust
let maturity_modulation = match self
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{
    None => return,   // ← all spawning neurons are frozen here
    Some(value) => value,
};
``` [4](#0-3) 

Neurons already in `Spawning` state (their parent's maturity has already been transferred to them) cannot be finalized. The child neuron's maturity is locked with no escape path.

**Step 5 — `finalize_maturity_disbursement` also hard-gates on `maturity_modulation`.**

`next_maturity_disbursement_to_finalize` returns `Err(NoMaturityModulation)` when the field is `None`:

```rust
let maturity_modulation_basis_points = maturity_modulation_basis_points
    .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;
``` [5](#0-4) 

This is confirmed by the test `test_finalize_maturity_disbursement_no_maturity_modulation`, which explicitly verifies that disbursements fail with `NoMaturityModulation` when the field is absent. [6](#0-5) 

**Step 6 — SNS governance has the same pattern.**

`maybe_finalize_disburse_maturity` in SNS governance returns early when `effective_maturity_modulation_basis_points()` returns `Err` (i.e., when `current_basis_points` is `None`):

```rust
Err(message) => {
    log!(ERROR, "{}", message.error_message);
    return;
}
``` [7](#0-6) 

---

### Impact Explanation

Neurons placed in `Spawning` state have already had their maturity moved from the parent neuron. If `maybe_spawn_neurons` is permanently blocked (because `current_value_permyriad` is `None`), the child neuron's maturity is frozen indefinitely:

- The user cannot disburse, cancel, or otherwise recover the maturity from a `Spawning` neuron.
- Maturity disbursements queued via `DisburseMaturity` are also frozen.
- On a fresh NNS or SNS governance canister, this window lasts until the XRC backfill completes (~30 minutes at 5 s/day × 365 days), but if the XRC is persistently unavailable or returns zero rates, the freeze is permanent.

The `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` sanity check at line 6438 adds a second freeze path: if a stored modulation value falls outside `[-1000, 200]`, spawning is also silently skipped. [8](#0-7) 

---

### Likelihood Explanation

Three realistic scenarios trigger this:

1. **Fresh governance canister** — `current_value_permyriad` starts as `None` (protobuf default). Any `spawn_neuron` call during the ~30-minute XRC backfill window creates a neuron that cannot be finalized until backfill completes. If backfill fails (XRC unreachable), the freeze is indefinite.

2. **XRC canister unavailable** — The XRC canister is an HTTP-outcall-based system canister. If it is unreachable or returns `StablecoinRateZeroRate` / `CryptoBaseAssetNotFound` errors persistently, no new rates are stored, and on a canister that has never had a rate, `current_value_permyriad` stays `None`.

3. **ICP price collapses to near-zero** — If the 365-day average `xdr_permyriad_per_icp` rounds to zero (e.g., ICP price crashes), `compute_maturity_modulation_permyriad` returns `Err("reference price averaged to zero")`, and the modulation is never updated from its last known value. If the last known value was itself `None` (fresh canister), spawning is permanently blocked.

---

### Recommendation

1. **Provide a safe fallback modulation of `0` (no modulation) when `current_value_permyriad` is `None`**, rather than blocking all spawning and disbursement. A zero modulation means maturity converts 1:1 to ICP, which is the neutral/safe default.

2. **Add a staleness check**: if `updated_at_days_since_epoch` is more than N days old, treat the modulation as unavailable and use the fallback, rather than silently using a potentially very stale value.

3. **Emit a governance metric or alert** when spawning neurons are present but `current_value_permyriad` is `None`, so operators can detect the freeze.

---

### Proof of Concept

```
1. Deploy a fresh NNS governance canister (maturity_modulation = None).
2. User A calls spawn_neuron() on a neuron with sufficient maturity.
   → Child neuron created in Spawning state; parent maturity reduced.
3. XRC canister is unreachable (or returns zero rates for all days).
   → fetch_and_validate_rate() returns None for every backfill attempt.
   → update_maturity_modulation() skips update; current_value_permyriad stays None.
4. spawn_at_timestamp_seconds elapses (7 days later).
5. maybe_spawn_neurons() is called by the periodic timer.
   → Line 6433: current_value_permyriad is None → return early.
   → Child neuron remains in Spawning state; ICP is never minted.
6. User A has no recourse: the spawning neuron cannot be cancelled,
   disbursed, or otherwise recovered. Maturity is permanently locked.
``` [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L149-151)
```rust
    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L417-427)
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
```

**File:** rs/nns/governance/src/governance.rs (L276-278)
```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L457-458)
```rust
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;
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

**File:** rs/sns/governance/src/governance.rs (L4926-4933)
```rust
        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };
```
