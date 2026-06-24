### Title
ICP Ledger Deduplication Disabled for Governance Mints via Forced `created_at_time` Substitution - (File: `rs/ledger_suite/icp/ledger/src/lib.rs`)

### Summary
The ICP ledger's `add_payment_with_timestamp` always substitutes `created_at_time = now` when callers pass `None`, making every transaction hash unique and rendering ledger-level deduplication inoperative for those callers. The ICP ledger deduplication test is explicitly disabled (`#[ignore = "requires fix for FI-541"]`). All governance-initiated mints — including the Neurons' Fund ICP mint to SNS treasury via `mint_to_sns_governance` — pass `created_at_time: None` through `IcpLedgerCanister::transfer_funds`. This means the only deduplication guard is the NNS Governance lifecycle state machine. If that application-level guard fails to prevent a retry (e.g., lifecycle is set to terminal before the mint, mint fails, lifecycle is restored), a second call to `mint_to_sns_governance` in a different round will produce a distinct transaction hash and succeed, minting ICP a second time.

### Finding Description

**Root cause 1 — ICP ledger always substitutes `now` for `None`:**

In `add_payment_with_timestamp`, the ICP ledger unconditionally fills in the current timestamp when the caller omits `created_at_time`:

```rust
// TODO(FI-349): preserve created_at_time and memo the caller specified.
created_at_time: created_at_time.or(Some(now)),
``` [1](#0-0) 

Because the deduplication hash in `apply_transaction` is computed over the full transaction (including `created_at_time`), every call with `None` receives a unique hash keyed to the wall-clock instant of that round. Two calls in different rounds therefore always produce different hashes and both succeed — the deduplication window is populated but never matched. [2](#0-1) 

**Root cause 2 — ICP ledger deduplication test is disabled:**

The integration test that validates deduplication for the ICP ledger is suppressed with a known bug ticket:

```rust
#[ignore = "requires fix for FI-541"]
#[test]
fn test_tx_deduplication() { ... }
``` [3](#0-2) 

**Root cause 3 — `IcpLedgerCanister::transfer_funds` always passes `created_at_time: None`:**

The shared nervous-system ledger client used by NNS Governance, SNS Governance, and the Swap canister unconditionally omits `created_at_time`:

```rust
created_at_time: None,
``` [4](#0-3) 

**Root cause 4 — `mint_to_sns_governance` has no ledger-level deduplication:**

When a committed SNS swap settles, NNS Governance mints ICP to the SNS treasury with a fixed `memo = 0` and no `created_at_time`:

```rust
let _ = self
    .ledger
    .transfer_funds(
        amount_icp_e8s,
        /* fee_e8s = */ 0,
        /* from_subaccount = */ None,
        destination,
        /* memo = */ 0,
    )
    .await
``` [5](#0-4) 

**Root cause 5 — lifecycle is set to terminal *before* the mint:**

The proposal lifecycle is advanced to `Committed`/`Aborted` at line 7235, before `mint_to_sns_governance` is awaited at line 7266. If the mint call returns an error, the comment at line 7047 states the previous lifecycle "should be set to allow for retries." If that restoration succeeds and the caller retries `settle_neurons_fund_participation`, the state machine re-enters "Ok case III" (line 7180) and calls `mint_to_sns_governance` again. Because each call lands in a different round with a different `now`, the ICP ledger accepts both mints. [6](#0-5) [7](#0-6) [8](#0-7) 

### Impact Explanation

A duplicate successful call to `mint_to_sns_governance` mints ICP tokens from the NNS minting account to the SNS governance treasury a second time. This inflates the ICP supply by the Neurons' Fund participation amount (potentially millions of ICP e8s) without any corresponding maturity deduction, violating the ICP ledger conservation invariant. The SNS treasury receives double the intended ICP, and the Neurons' Fund neurons are only debited once.

**Impact: Medium** — requires a transient ledger failure followed by a successful retry, but the ledger-level deduplication that should serve as a safety net is confirmed broken (FI-541, test disabled).

### Likelihood Explanation

The SNS Swap `finalize_swap` endpoint is callable by any principal. The `settle_neurons_fund_participation` path inside NNS Governance is reachable from any committed SNS swap. The ICP ledger's deduplication is known to be broken (FI-541). The lifecycle restoration on mint failure is the only guard, and its correctness is not verified by any test covering the failure-then-retry path. Transient ledger unavailability (`TxThrottled`, replica errors) is a realistic trigger.

**Likelihood: Medium**

### Recommendation

1. **Mirror the deduplication key in the mint call:** Pass a stable, proposal-scoped `created_at_time` (e.g., derived from the proposal ID or a stored timestamp) and a unique memo in `mint_to_sns_governance` so the ICP ledger can reject a duplicate mint even if the application-level guard fails.
2. **Fix FI-541 and re-enable `test_tx_deduplication`** for the ICP ledger to restore the deduplication safety net.
3. **Add an assertion** in `settle_neurons_fund_participation` that verifies the final participation has not already been stored before calling `mint_to_sns_governance`, analogous to the recommendation to add a startup assertion for the deduplication index.
4. **Audit the lifecycle restoration path** to confirm it cannot leave the proposal in a state where `mint_to_sns_governance` is called more than once for the same swap.

### Proof of Concept

1. A committed SNS swap calls `finalize_swap` → `settle_neurons_fund_participation` on NNS Governance.
2. NNS Governance validates the request, sets the proposal lifecycle to `

### Citations

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L405-406)
```rust
                // TODO(FI-349): preserve created_at_time and memo the caller specified.
                created_at_time: created_at_time.or(Some(now)),
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L233-253)
```rust
    let maybe_time_and_hash = transaction
        .created_at_time()
        .map(|created_at_time| (created_at_time, transaction.hash()));

    if let Some((created_at_time, tx_hash)) = maybe_time_and_hash {
        // The caller requested deduplication.
        if created_at_time + ledger.transaction_window() < now {
            return Err(TransferError::TxTooOld {
                allowed_window_nanos: ledger.transaction_window().as_nanos() as u64,
            });
        }

        if created_at_time > now + ic_limits::PERMITTED_DRIFT {
            return Err(TransferError::TxCreatedInFuture { ledger_time: now });
        }

        if let Some(block_height) = ledger.transactions_by_hash().get(&tx_hash) {
            return Err(TransferError::TxDuplicate {
                duplicate_of: *block_height,
            });
        }
```

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L454-458)
```rust
#[ignore = "requires fix for FI-541"]
#[test]
fn test_tx_deduplication() {
    ic_ledger_suite_state_machine_tests::test_tx_deduplication(ledger_wasm(), encode_init_args);
}
```

**File:** rs/nervous_system/canisters/src/ledger.rs (L124-131)
```rust
            (TransferArgs {
                memo: Memo(memo),
                amount: Tokens::from_e8s(amount_e8s),
                fee: Tokens::from_e8s(fee_e8s),
                from_subaccount,
                to: to.to_address(),
                created_at_time: None,
            },),
```

**File:** rs/nns/governance/src/governance.rs (L7180-7185)
```rust
            (Some(_), None, None) => {
                // Ok case III: This function invocation should compute the Neurons' Fund
                // participation, mint ICP to SNS treasury, refund the leftovers, and return
                // the (newly computed) Neurons' Fund participants.
                // Nothing to do.
            }
```

**File:** rs/nns/governance/src/governance.rs (L7234-7237)
```rust
        // Set the lifecycle of the proposal to avoid interleaving callers.
        proposal_data.set_swap_lifecycle_by_settle_neurons_fund_participation_request_type(
            &request.swap_result,
        );
```

**File:** rs/nns/governance/src/governance.rs (L7266-7273)
```rust
            let mint_icp_result = self
                .mint_to_sns_governance(
                    &request.nns_proposal_id,
                    sns_governance_canister_id,
                    swap_estimated_total_neurons_fund_participation_icp_e8s,
                    amount_icp_e8s,
                )
                .await;
```

**File:** rs/nns/governance/src/governance.rs (L7498-7506)
```rust
        let _ = self
            .ledger
            .transfer_funds(
                amount_icp_e8s,
                /* fee_e8s = */ 0, // Because there is no fee for minting.
                /* from_subaccount = */ None,
                destination,
                /* memo = */ 0,
            )
```
