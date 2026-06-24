Audit Report

## Title
SNS Governance Maturity Disbursement Permanently Blocked When CMC Is Unavailable - (File: rs/sns/governance/src/governance.rs)

## Summary

SNS Governance immediately deducts maturity from a neuron when `disburse_maturity` is called, but defers the actual minting to a periodic task (`maybe_finalize_disburse_maturity`) that requires a valid `current_basis_points` value fetched from the CMC. If the CMC has never successfully returned a value, `current_basis_points` remains `None`, causing the finalization loop to silently return on every periodic tick. The maturity is removed from the neuron but never minted to the destination account for as long as the CMC remains unreachable. NNS Governance was patched (Proposal 141779) to seed a neutral `0` at init; SNS Governance has no equivalent safeguard.

## Finding Description

**Step 1 — Immediate deduction.** When `disburse_maturity` is called, the neuron's `maturity_e8s_equivalent` is immediately reduced and a `DisburseMaturityInProgress` record is pushed: [1](#0-0) 

**Step 2 — Deferred finalization blocked by `None`.** `maybe_finalize_disburse_maturity` calls `effective_maturity_modulation_basis_points()` at the top of its body and returns early on `Err`: [2](#0-1) 

**Step 3 — `Err` path when `current_basis_points` is `None`.** `effective_maturity_modulation_basis_points()` returns `Err` whenever `maturity_modulation.current_basis_points` is `None`: [3](#0-2) 

**Step 4 — CMC failure leaves state unchanged.** `update_maturity_modulation` silently returns without writing any value if the CMC call fails: [4](#0-3) 

**Step 5 — Proto field starts as `None`.** The `MaturityModulation.current_basis_points` field is `optional int32`, so it is `None` on a fresh SNS deployment: [5](#0-4) 

**Step 6 — No init seeding in SNS Governance.** A grep for any `maturity_modulation = Some(...)` assignment in `rs/sns/governance/src/` returns no matches, confirming there is no neutral seed at canister initialization. By contrast, NNS Governance explicitly seeds `maturity_modulation` with `current_value_permyriad: Some(0)` at init: [6](#0-5) 

**Step 7 — NNS test confirms the failure mode.** The NNS test `test_finalize_maturity_disbursement_no_maturity_modulation` explicitly sets `maturity_modulation = None` and asserts that finalization returns `Err(FinalizeMaturityDisbursementError::NoMaturityModulation)`, proving the code path is real and exercised: [7](#0-6) 

The NNS fix (Proposal 141779) migrated NNS to a locally computed XRC-backed modulation and seeded it with `0`; the analogous protection was never applied to SNS Governance: [8](#0-7) 

## Impact Explanation

A neuron owner who calls `DisburseMaturity` on an SNS whose CMC polling has never succeeded will have their maturity permanently deducted from the neuron with no corresponding ICP minted to the destination account. The `disburse_maturity_in_progress` entry persists indefinitely; the maturity is not held anywhere — it is simply never minted. This constitutes a concrete, user-visible loss of SNS maturity rewards and breaks the accounting invariant that deducted maturity must eventually be minted. Recovery requires a privileged governance upgrade. This matches the allowed impact: **High — Significant SNS security impact with concrete user or protocol harm** (unauthorized/unrecoverable loss of governance reward assets without a privileged upgrade path available to the affected user).

## Likelihood Explanation

The precondition is that `current_basis_points` is `None` at the time `maybe_finalize_disburse_maturity` runs after the 7-day delay. This occurs on any SNS where CMC polling has never succeeded — e.g., a newly deployed SNS before the first successful CMC poll, an SNS with a misconfigured CMC canister ID, or one deployed during a CMC upgrade window. The NNS Governance canister was explicitly patched for this exact scenario (Proposal 141779), confirming DFINITY recognized it as a real operational risk. The exploit is triggered by an unprivileged `manage_neuron` ingress call available to any neuron controller. **Likelihood: Medium.**

## Recommendation

1. **Seed at init:** In SNS Governance canister initialization, set `proto.maturity_modulation = Some(MaturityModulation { current_basis_points: Some(0), updated_at_timestamp_seconds: None })`, mirroring the NNS fix.
2. **Fallback in finalization:** In `maybe_finalize_disburse_maturity`, fall back to `0` basis points (identity modulation) rather than returning early when `effective_maturity_modulation_basis_points()` returns `Err`, so that already-deducted maturity is never permanently stranded.

## Proof of Concept

1. Deploy a fresh SNS with a stopped or misconfigured CMC canister so that `update_maturity_modulation` always fails and `current_basis_points` remains `None`.
2. Earn maturity on a neuron (via voting rewards).
3. Call `manage_neuron` with `Command::DisburseMaturity { percentage_to_disburse: 100, to_account: None }` as the neuron controller.
4. Observe: `neuron.maturity_e8s_equivalent` drops to `0`; `neuron.disburse_maturity_in_progress` gains one entry. The call returns `Ok`.
5. Advance time past `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days).
6. Observe: `run_periodic_tasks` fires `maybe_finalize_disburse_maturity`; `effective_maturity_modulation_basis_points()` returns `Err` (logged at ERROR level); the function returns immediately.
7. The destination account balance never increases. The `disburse_maturity_in_progress` entry remains indefinitely. The maturity is gone from the neuron and never minted.

A minimal unit test mirrors `test_finalize_maturity_disbursement_no_maturity_modulation` from NNS but applied to the SNS governance path, asserting that `maybe_finalize_disburse_maturity` returns without minting when `proto.maturity_modulation` is `None`.

### Citations

**File:** rs/sns/governance/src/governance.rs (L417-428)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L1692-1698)
```rust
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
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

**File:** rs/sns/governance/src/governance.rs (L5695-5701)
```rust
        // Fetch new maturity modulation.
        let maturity_modulation = self.cmc.neuron_maturity_modulation().await;

        // Unwrap response.
        let Ok(maturity_modulation) = maturity_modulation else {
            return;
        };
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1683-1689)
```text
    optional int32 current_basis_points = 1;

    // When current_basis_points was last updated (seconds since UNIX epoch).
    optional uint64 updated_at_timestamp_seconds = 2;
  }

  MaturityModulation maturity_modulation = 26;
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

**File:** rs/nns/governance/src/governance/disburse_maturity_tests.rs (L612-650)
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
```

**File:** rs/nns/governance/CHANGELOG.md (L14-22)
```markdown
# 2026-05-17: Proposal 141779

http://dashboard.internetcomputer.org/proposal/141779

## Changed

* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```
