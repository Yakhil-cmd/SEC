### Title
Unconditional Zero-Amount ICP Transfer in `error_refund_icp` Causes Unnecessary Ledger Rejection - (`rs/sns/swap/src/swap.rs`)

---

### Summary

The `error_refund_icp` function in the SNS Swap canister computes a refund amount via `saturating_sub` and then unconditionally calls `transfer_funds` without checking whether the resulting amount is zero. When a caller's subaccount balance is less than or equal to `DEFAULT_TRANSFER_FEE`, the transfer amount becomes 0, the ICP ledger rejects it, and the function returns a misleading external error instead of gracefully indicating there is nothing to refund. This is the direct IC analog of the Caviar `PrivatePool` bug: an unconditional transfer of a potentially-zero amount that the underlying ledger rejects.

---

### Finding Description

In `error_refund_icp`, after querying the on-chain balance of the caller's subaccount, the code computes:

```rust
let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
```

and then immediately calls:

```rust
let transfer_result = icp_ledger
    .transfer_funds(
        amount_e8s,
        DEFAULT_TRANSFER_FEE.get_e8s(),
        Some(source_subaccount),
        dst,
        0, // memo
    )
    .await;
``` [1](#0-0) 

There is **no guard** checking `amount_e8s > 0` before the call. When `balance_e8s <= DEFAULT_TRANSFER_FEE.get_e8s()` (10,000 e8s = 0.0001 ICP), `saturating_sub` yields 0, and `transfer_funds(0, fee, ...)` is dispatched to the ICP ledger. The ICP ledger rejects zero-amount transfers, causing the function to fall into the `Err` branch and return `ErrorRefundIcpResponse::new_external_error(...)`. [2](#0-1) 

This behavior is explicitly acknowledged in the integration test suite:

> "Currently, Swap.error_refund_icp returns an error from ICP Ledger if the amount to reimburse is zero (or less than the transfer fee)." [3](#0-2) 

The root cause is the absence of a zero-check before the ledger call, identical in structure to the Caviar `PrivatePool` bug where `ERC20.safeTransferFrom` was called unconditionally even when `feeAmount == 0`.

---

### Impact Explanation

Any unprivileged user who has a subaccount balance of exactly `DEFAULT_TRANSFER_FEE` (10,000 e8s) or less in the Swap canister's subaccount — reachable after the swap enters `ABORTED` or `COMMITTED` state — will receive a misleading `TYPE_EXTERNAL` error when calling `error_refund_icp`. The error message says "Transfer request failed" rather than "nothing to refund," making it indistinguishable from a genuine ledger outage. The user's funds (up to 9,999 e8s) are permanently stranded in the Swap canister's subaccount with no recovery path, since the only refund mechanism fails unconditionally for this balance range.

The `transfer_helper` used by `sweep_icp` correctly guards against this with `if amount <= fee { return TransferResult::AmountTooSmall; }`, but `error_refund_icp` has no equivalent guard. [4](#0-3) 

---

### Likelihood Explanation

**Medium.** The scenario is reachable by any unprivileged ingress caller after a swap finalizes. It occurs when:
1. A user accidentally sends exactly `DEFAULT_TRANSFER_FEE` (or less) to the Swap canister's subaccount without calling `refresh_buyer_tokens`, or
2. A user's balance was partially consumed by a prior sweep, leaving a dust amount ≤ the fee.

No privileged access, governance majority, or threshold attack is required. The caller only needs to be a valid principal and the swap must be in `ABORTED` or `COMMITTED` state.

---

### Recommendation

Add a zero-amount guard before the `transfer_funds` call in `error_refund_icp`:

```rust
// Make transfer.
let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
if amount_e8s == 0 {
    return ErrorRefundIcpResponse::new_precondition_error(
        "Nothing to refund: balance does not exceed the transfer fee.",
    );
}
let transfer_result = icp_ledger
    .transfer_funds(
        amount_e8s,
        DEFAULT_TRANSFER_FEE.get_e8s(),
        Some(source_subaccount),
        dst,
        0,
    )
    .await;
```

This mirrors the guard already present in `TransferableAmount::transfer_helper`. [4](#0-3) 

---

### Proof of Concept

1. A swap enters `COMMITTED` or `ABORTED` state.
2. A user (`P`) has a subaccount balance of exactly `10_000` e8s in the Swap canister (e.g., they accidentally sent the exact fee amount without calling `refresh_buyer_tokens`).
3. `P` calls `error_refund_icp({ source_principal_id: Some(P) })`.
4. The canister queries the balance: `balance_e8s = 10_000`.
5. `amount_e8s = 10_000u64.saturating_sub(10_000) = 0`.
6. `icp_ledger.transfer_funds(0, 10_000, Some(subaccount), dst, 0)` is called.
7. The ICP ledger rejects the call (zero-amount transfer).
8. The Swap canister returns `ErrorRefundIcpResponse { result: Err { error_type: TYPE_EXTERNAL, description: "Transfer request failed: ..." } }`.
9. `P`'s 10,000 e8s remain permanently stranded in the Swap canister's subaccount with no recovery path. [1](#0-0)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1990-2004)
```rust
        // Make transfer.
        let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
        let dst = Account {
            owner: source_principal_id.0,
            subaccount: None,
        };
        let transfer_result = icp_ledger
            .transfer_funds(
                amount_e8s,
                DEFAULT_TRANSFER_FEE.get_e8s(),
                Some(source_subaccount),
                dst,
                0, // memo
            )
            .await;
```

**File:** rs/sns/swap/src/swap.rs (L2019-2030)
```rust
            Err(err) => {
                log!(
                    ERROR,
                    "Error refund - failed to transfer {} from subaccount {:#?}: {}",
                    amount_e8s,
                    source_subaccount,
                    err,
                );
                ErrorRefundIcpResponse::new_external_error(format!(
                    "Transfer request failed: {err}",
                ))
            }
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L852-884)
```rust
        // Notes to help understand this spec:
        // 1. Currently, Swap.error_refund_icp returns an error from ICP Ledger if the amount
        //    to reimburse is zero (or less than the transfer fee).
        // 2. Currently, when `ensure_swap_timeout_is_reached` is true, none of the direct
        //    participants call Swap.refresh_buyer_tokens before the timeout, so their ICP is still
        //    to be refunded by calling Swap.error_refund_icp (case A).
        // 3. Conversely, when `ensure_swap_timeout_is_reached` is false and
        //    `expect_swap_overcommitted` is true, Swap.sweep_icp takes care of all the refunds,
        //    so there's no more refunds that can happen in Swap.error_refund_icp, which thus
        //    returns an error (case B).
        let expected_refund_e8s = if ensure_swap_timeout_is_reached {
            // Case A: Expecting to get refunded with Transferred - (ICP Ledger transfer fee).
            assert_matches!(
                error_refund_icp_result,
                error_refund_icp_response::Result::Ok(_)
            );

            attempted_participation_amount_e8s - DEFAULT_TRANSFER_FEE.get_e8s()
        } else if accepted_participation_amount_e8s == 0
            || accepted_participation_amount_e8s == attempted_participation_amount_e8s
        {
            // Case B: (No tokens accepted) || (All tokens accepted)  ==>  nothing to refund.

            let error_text = assert_matches!(
                error_refund_icp_result,
                error_refund_icp_response::Result::Err(err) => {
                    err.description.expect("ICP Ledger errors should have a description.")
                }
            );
            assert!(error_text.contains(
                "the debit account doesn't have enough funds to complete the transaction"
            ));

```

**File:** rs/sns/swap/src/types.rs (L612-616)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
```
