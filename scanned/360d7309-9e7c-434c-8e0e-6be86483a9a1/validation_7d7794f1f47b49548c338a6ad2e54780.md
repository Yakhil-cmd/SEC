### Title
SNS Governance Maturity Disbursement Permanently Blocked When CMC Is Unavailable - (File: rs/sns/governance/src/governance.rs)

### Summary

In SNS Governance, when a user calls `disburse_maturity`, the maturity is immediately deducted from the neuron and queued as a `DisburseMaturityInProgress` entry. The actual minting transfer is deferred to a periodic timer task (`maybe_finalize_disburse_maturity`). That task requires a valid `maturity_modulation.current_basis_points` fetched from the Cycles Minting Canister (CMC). If the CMC is unavailable and has never successfully returned a value, `current_basis_points` remains `None`, causing `effective_maturity_modulation_basis_points()` to return `Err`, and the entire disbursement loop silently returns without processing any pending disbursements. Users' maturity is permanently stuck — already deducted from the neuron, but never minted to the destination account — for as long as the CMC remains unreachable.

### Finding Description

When a user calls `DisburseMaturity` on an SNS neuron, the SNS Governance canister immediately deducts `maturity_to_deduct` from `neuron.maturity_e8s_equivalent` and pushes a `DisburseMaturityInProgress` record: [1](#0-0) 

The actual minting is deferred to `maybe_finalize_disburse_maturity`, which runs as a periodic task. At the very start of that function, it calls `effective_maturity_modulation_basis_points()`: [2](#0-1) 

`effective_maturity_modulation_basis_points()` returns `Err` when `maturity_modulation.current_basis_points` is `None`: [3](#0-2) 

The CMC polling function `update_maturity_modulation` silently returns without updating state if the CMC call fails: [4](#0-3) 

The SNS `MaturityModulation` proto field `current_basis_points` is `optional int32`, meaning it starts as `None` on a fresh SNS deployment: [5](#0-4) 

Unlike NNS Governance (which was patched in Proposal 141779 to seed `maturity_modulation` with a neutral `0` at init), SNS Governance has no such seed. If the CMC is unreachable from the time the SNS is deployed until after a user initiates a disbursement, `current_basis_points` stays `None`, and `maybe_finalize_disburse_maturity` returns early on every periodic tick: [6](#0-5) 

The maturity has already been removed from the neuron but the minting transfer never executes. There is no fallback, no retry with a default modulation of 0, and no way for the user to recover the funds without a governance upgrade.

The NNS Governance test `test_finalize_maturity_disbursement_no_maturity_modulation` explicitly confirms this failure mode exists: [7](#0-6) 

### Impact Explanation

A neuron owner who calls `DisburseMaturity` on an SNS whose CMC polling has never succeeded (e.g., a newly deployed SNS, or one whose CMC canister ID is misconfigured) will have their maturity permanently deducted from the neuron with no corresponding ICP minted. The funds are not held anywhere — they are simply never minted. The invariant that "maturity deducted from a neuron must eventually be minted to the destination account" is broken. Recovery requires a governance upgrade to either seed the modulation value or manually process the pending disbursements.

**Impact: Medium** — Users lose access to maturity rewards for an indefinite period; recovery requires a privileged canister upgrade.

### Likelihood Explanation

**Likelihood: Medium** — Any SNS that is deployed and whose CMC polling fails before the first `DisburseMaturity` call is made is vulnerable. CMC unavailability can occur transiently (e.g., during subnet upgrades, CMC canister upgrades, or network partitions). The NNS Governance canister was explicitly patched (Proposal 141779) to seed a neutral `0` value at init precisely because this scenario was recognized as a real risk — but the analogous fix was not applied to SNS Governance. [8](#0-7) [9](#0-8) 

### Recommendation

SNS Governance should seed `proto.maturity_modulation` with a neutral `current_basis_points: Some(0)` during canister initialization, mirroring the fix applied to NNS Governance. Additionally, `maybe_finalize_disburse_maturity` should fall back to `0` basis points (identity modulation) rather than silently skipping all pending disbursements when the CMC value is unavailable, so that already-deducted maturity is never permanently stranded.

### Proof of Concept

1. Deploy a fresh SNS whose CMC canister is unreachable (e.g., wrong CMC canister ID, or CMC is stopped).
2. Earn maturity on a neuron (via voting rewards).
3. Call `manage_neuron` with `Command::DisburseMaturity { percentage_to_disburse: 100, to_account: None }`.
4. Observe: `neuron.maturity_e8s_equivalent` drops to `0` and `neuron.disburse_maturity_in_progress` gains one entry. The call returns `Ok`.
5. Advance time past `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days).
6. Observe: `run_periodic_tasks` fires `maybe_finalize_disburse_maturity`, which calls `effective_maturity_modulation_basis_points()`, gets `Err` because `current_basis_points` is `None`, logs the error, and returns immediately.
7. The destination account balance never increases. The maturity is gone from the neuron and never minted. The `disburse_maturity_in_progress` entry remains indefinitely.

The entry point is the unprivileged `manage_neuron` ingress call available to any neuron controller: [10](#0-9)

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

**File:** rs/sns/governance/src/governance.rs (L1609-1616)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1675-1689)
```text
  message MaturityModulation {
    // When X maturity is disbursed, the amount that goes to the destination
    // account is X * (1 + y) where y = current_basis_points / 10_000.
    //
    // Fetched from the cycles minting canister (same as NNS governance).
    //
    // There is a positive relationship between the price of ICP (in XDR) and
    // this value.
    optional int32 current_basis_points = 1;

    // When current_basis_points was last updated (seconds since UNIX epoch).
    optional uint64 updated_at_timestamp_seconds = 2;
  }

  MaturityModulation maturity_modulation = 26;
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
