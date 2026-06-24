### Title
Excess ICP Not Refunded During OPEN State in SNS Swap `refresh_buyer_token_e8s` - (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS Swap canister's `refresh_buyer_token_e8s` function accepts only a capped portion of a buyer's ICP deposit (bounded by `max_participant_icp_e8s` or `available_direct_participation_e8s()`), but does not refund the excess ICP to the buyer. The excess remains locked in the buyer's subaccount of the swap canister for the entire OPEN lifecycle, which can span days or weeks. Recovery requires the buyer to call `error_refund_icp` only after the swap reaches COMMITTED or ABORTED state — a non-obvious, manual step. The codebase itself acknowledges this with an open TODO.

### Finding Description

In `refresh_buyer_token_e8s`, the function reads the full ICP balance `e8s` from the buyer's subaccount on the ICP ledger, then computes the accepted amount:

```
actual_increment_e8s = min(max_increment_e8s, requested_increment_e8s)
new_balance_e8s      = min(old_amount + actual_increment, max_participant_icp_e8s)
```

Only `new_balance_e8s` is recorded in `BuyerState`. The difference `e8s - new_balance_e8s` (the excess ICP) is never returned to the buyer during the OPEN state. The function returns `Ok(...)` without issuing any refund transfer.

The code even carries an explicit acknowledgment of this gap:

```rust
/// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
pub async fn refresh_buyer_token_e8s(
``` [1](#0-0) 

The capping logic that produces the excess: [2](#0-1) 

The function returns successfully with no refund of the excess: [3](#0-2) 

The only recovery path, `error_refund_icp`, is gated behind the swap being COMMITTED or ABORTED: [4](#0-3) 

When `sweep_icp` runs at finalization, it transfers only `icp_transferable_amount.amount_e8s` (the accepted amount) to SNS governance or back to the buyer — not the full subaccount balance — leaving the excess in the subaccount: [5](#0-4) 

### Impact Explanation

A buyer who sends 10 ICP when only 4 ICP can be accepted (e.g., the swap is nearly full, or `max_participant_icp_e8s` is 4 ICP) has 6 ICP locked in the swap canister's subaccount for the entire OPEN period. The buyer:

1. Cannot retrieve the excess during OPEN state — `error_refund_icp` rejects with `"Error refunds can only be performed when the swap is ABORTED or COMMITTED"`.
2. Must know to call `error_refund_icp` after the swap closes — this is not automatic.
3. Suffers capital lockup for the duration of the swap (potentially weeks).
4. If they never call `error_refund_icp`, the excess ICP sits in the subaccount indefinitely (though it remains recoverable).

The integration test spec explicitly documents this as a known behavior requiring a separate manual step: [6](#0-5) 

### Likelihood Explanation

This is triggered by any buyer who sends more ICP than the swap can accept — a common scenario when:
- The swap is nearly at `max_direct_participation_icp_e8s` and a new buyer sends a full `max_participant_icp_e8s`.
- A buyer sends more than `max_participant_icp_e8s` to ensure their participation succeeds.

No special privileges are required. Any unprivileged ingress sender can trigger this by transferring ICP to their swap subaccount and calling `refresh_buyer_tokens`.

### Recommendation

Inside `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, if `e8s > new_balance_e8s`, immediately issue an ICP ledger transfer to return `e8s - new_balance_e8s` (minus the transfer fee) to the buyer's principal account. This mirrors the behavior already implemented for the ETH-base-token case in the referenced ERC20 report and resolves the open TODO at line 1133.

### Proof of Concept

1. A swap is OPEN with `max_direct_participation_icp_e8s = 10 ICP` and `max_participant_icp_e8s = 6 ICP`. User 1 has already deposited 6 ICP.
2. User 2 transfers 6 ICP to their subaccount of the swap canister on the ICP ledger.
3. User 2 calls `refresh_buyer_tokens`. The swap has only 4 ICP of capacity left (`available_direct_participation_e8s() = 4`). The function accepts 4 ICP and records `BuyerState { amount_e8s: 4 ICP }`. The remaining 2 ICP stays in User 2's subaccount.
4. User 2 immediately calls `error_refund_icp` to recover the 2 ICP — the call is rejected: `"Error refunds can only be performed when the swap is ABORTED or COMMITTED"`.
5. The 2 ICP remains locked until the swap closes. User 2 must then manually call `error_refund_icp` to recover it, paying an additional transfer fee.

This is confirmed by the test `test_max_icp` which shows 6 ICP sent but only 4 ICP accepted with no refund issued: [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1133-1134)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1222-1237)
```rust
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
        if new_balance_e8s > max_participant_icp_e8s {
            log!(
                INFO,
                "Participant {} contributed {} e8s - the limit per participant is {}",
                buyer,
                new_balance_e8s,
                max_participant_icp_e8s
            );
        }

        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1308-1312)
```rust
        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: new_balance_e8s,
            icp_ledger_account_balance_e8s: e8s,
        })
    }
```

**File:** rs/sns/swap/src/swap.rs (L1931-1936)
```rust
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L2113-2121)
```rust
            let result = icp_transferable_amount
                .transfer_helper(
                    now_fn,
                    DEFAULT_TRANSFER_FEE,
                    Some(subaccount),
                    &dst,
                    icp_ledger,
                )
                .await;
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L138-141)
```rust
///     3. ICP refunding mechanism and ICP balances:
///         1. `{ true } FinalizeUnSuccessfully; Swap.error_refund_icp() { All directly participated ICP (minus the fees) are refunded. }`
///         2. `{ true } FinalizeSuccessfully;   Swap.error_refund_icp() { Excess directly participated ICP (minus the fees) are refunded. }`
///
```

**File:** rs/sns/swap/tests/swap.rs (L529-563)
```rust
    // Deposit 6 ICP from another buyer.
    assert!(
        swap.refresh_buyer_token_e8s(
            *TEST_USER2_PRINCIPAL,
            None,
            SWAP_CANISTER_ID,
            &mock_stub(vec![LedgerExpect::AccountBalance(
                Account {
                    owner: SWAP_CANISTER_ID.get().into(),
                    subaccount: Some(principal_to_subaccount(&TEST_USER2_PRINCIPAL.clone()))
                },
                Ok(Tokens::from_e8s(6 * E8))
            )])
        )
        .now_or_never()
        .unwrap()
        .is_ok()
    );
    // But only 4 ICP is "accepted".
    assert_eq!(
        swap.buyers
            .get(&TEST_USER2_PRINCIPAL.to_string())
            .unwrap()
            .amount_icp_e8s(),
        4 * E8
    );
    // Can commit even if time isn't up as the max has been reached.
    assert!(swap.can_commit(END_TIMESTAMP_SECONDS - 1));
    // This should commit, and should not abort
    assert!(!swap.try_abort(END_TIMESTAMP_SECONDS - 1));
    assert!(swap.try_commit(END_TIMESTAMP_SECONDS - 1));
    assert_eq!(swap.lifecycle(), Committed);
    // Check that buyer balances are correct.
    verify_direct_participant_icp_balances(&swap, &TEST_USER1_PRINCIPAL, 6 * E8);
    verify_direct_participant_icp_balances(&swap, &TEST_USER2_PRINCIPAL, 4 * E8);
```
