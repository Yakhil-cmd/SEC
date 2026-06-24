Audit Report

## Title
NNS Governance Maturity Disbursement and Neuron Spawning Universally Blocked When XRC-Derived Maturity Modulation Is Absent - (File: `rs/nns/governance/src/governance/disburse_maturity.rs`)

## Summary

Following Proposal 141779, both `finalize_maturity_disbursement` and `maybe_spawn_neurons` unconditionally require `heap_data.maturity_modulation.current_value_permyriad` to be `Some(i32)`, a value populated exclusively by the `UpdateIcpXdrRateRelatedData` timer task via XRC. When this field is `None` — guaranteed on any fresh or reset governance canister during the ~30-minute backfill window, and indefinitely if XRC fails to return valid rates for any day in the 7-day recent window — every pending maturity disbursement and every spawning neuron across the entire NNS is frozen simultaneously with no administrative override.

## Finding Description

**Root cause.** `update_maturity_modulation` preserves the prior value on any computation error. If the prior value is `None` (fresh canister), it stays `None`. [1](#0-0) 

`compute_maturity_modulation_permyriad` returns `Err` whenever `compute_average_icp_xdr_rate` finds no rate in the 7-day recent window, which is the case on a fresh canister or after sustained XRC failures. [2](#0-1) 

**Path 1 — Maturity disbursement.** `try_finalize_maturity_disbursement` reads `current_value_permyriad` and passes it to `next_maturity_disbursement_to_finalize`: [3](#0-2) 

Inside that function, the first statement hard-fails on `None`: [4](#0-3) 

The outer `finalize_maturity_disbursement` logs the error and schedules a retry after `RETRY_INTERVAL` (60 s), but every retry produces the same result while modulation remains `None`. [5](#0-4) 

Critically, `initiate_maturity_disbursement` deducts maturity from the neuron immediately and atomically at initiation time: [6](#0-5) 

So the user's maturity is gone from the neuron but ICP is never minted until modulation becomes available.

**Path 2 — Neuron spawning.** `maybe_spawn_neurons` silently returns without minting any spawning neuron when the field is absent: [7](#0-6) 

**Regression.** The CHANGELOG confirms Proposal 141779 switched from the CMC-polled value (which initializes with a hardcoded default, making `None` unreachable in production) to the new XRC-derived local computation, which has no default: [8](#0-7) 

**No bypass.** Neither function exposes a governance proposal or privileged call to force finalization with a default modulation value. The SNS governance has a `maturity_modulation_disabled` escape hatch that returns `Ok(0)`: [9](#0-8) 

NNS governance has no equivalent.

**Test confirmation.** The existing test suite explicitly confirms the `NoMaturityModulation` failure path: [10](#0-9) 

## Impact Explanation

**Scope — all NNS users simultaneously.** A single missing field in global governance state blocks every pending maturity disbursement and every spawning neuron across the entire NNS. There is no per-neuron or per-user bypass.

**Fund lock-up.** A user who calls `disburse_maturity` has their maturity immediately deducted from the neuron. If finalization is blocked, the maturity is gone from the neuron but ICP is never minted. The user's funds are frozen for the duration of the outage (temporary but potentially indefinite if XRC failures persist).

**Spawning neuron lock-up.** A neuron placed in spawning state has its maturity zeroed. If `maybe_spawn_neurons` never runs successfully, the neuron remains in spawning state with no way for the owner to dissolve or otherwise manage it.

This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS and significant NNS security impact with concrete user or protocol harm.**

## Likelihood Explanation

**Guaranteed on every fresh or reset deployment.** The `UpdateIcpXdrRateRelatedData` task requires at least one successful XRC fetch for a day within the 7-day recent window before `compute_maturity_modulation_permyriad` can return `Ok`. At 5-second intervals the backfill takes ~30 minutes to attempt all 365 days. Any user who initiates a disbursement during this window and whose 7-day finalization deadline falls before the modulation is populated will be affected.

**Triggered by sustained XRC errors.** `fetch_and_validate_rate` returns `None` on any XRC call failure (network error, `Pending`, `RateLimited`, `StablecoinRateTooFewRates`, timestamp mismatch, zero rate). If every fetch in the 7-day recent window fails, `compute_maturity_modulation_permyriad` returns `Err("no rate available for the recent price window")` and the prior `None` is preserved indefinitely. [11](#0-10) 

**Introduced by a recent production change.** Before Proposal 141779 the CMC-polled value was always available. The new local computation has no such default, making the `None` state reachable in production for the first time.

## Recommendation

1. **Provide a safe default.** Initialize `maturity_modulation.current_value_permyriad` to `Some(0)` (neutral, no modulation) rather than `None`. This matches the pre-Mission-70 behavior and unblocks disbursements while the XRC history is being built.

2. **Decouple finalization from modulation availability.** When `current_value_permyriad` is `None`, `finalize_maturity_disbursement` should apply zero modulation (1:1 maturity-to-ICP) rather than returning an error, consistent with the SNS governance's `maturity_modulation_disabled` path.

3. **Retain the CMC-polled value as a fallback.** The old `cached_daily_maturity_modulation_basis_points` field could serve as a fallback when the XRC-derived value is absent, preventing a regression in availability.

## Proof of Concept

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
   → ICP is never minted. User A's maturity is deducted with no payout.
   → Timer retries after RETRY_INTERVAL; same result indefinitely.

5. User B had called spawn_neuron earlier; their neuron is in spawning state.
   maybe_spawn_neurons reads current_value_permyriad = None → returns immediately.
   → Spawning neuron remains frozen; User B cannot dissolve or manage it.

Reproducible as a unit test: set heap_data.maturity_modulation = None,
initiate a disbursement, advance time past DISBURSEMENT_DELAY_SECONDS,
call try_finalize_maturity_disbursement — asserts
Err(FinalizeMaturityDisbursementError::NoMaturityModulation).
This exact test already exists in disburse_maturity_tests.rs:612.
```

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L135-141)
```rust
    let recent_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the recent price window".to_string())?;

```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L265-309)
```rust
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L317-328)
```rust
    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
        .map_err(|_| InitiateMaturityDisbursementError::Unknown {
            reason: "Failed to update neuron even though it was found before".to_string(),
        })?;

    Ok(disbursement_maturity_e8s)
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L457-458)
```rust
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L547-553)
```rust
    match try_finalize_maturity_disbursement(governance).await {
        Ok(_) => governance.with_borrow(get_delay_until_next_finalization),
        Err(err) => {
            println!("FinalizeMaturityDisbursementTask failed: {}", err);
            RETRY_INTERVAL
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

**File:** rs/nns/governance/CHANGELOG.md (L20-23)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.

```

**File:** rs/sns/governance/src/governance.rs (L413-415)
```rust
        if maturity_modulation_disabled {
            return Ok(0);
        }
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
