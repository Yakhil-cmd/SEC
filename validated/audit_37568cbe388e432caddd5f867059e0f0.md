### Title
Excess ICP Stranded in SNS Swap Canister Subaccounts After `refresh_buyer_token_e8s` — (File: rs/sns/swap/src/swap.rs)

---

### Summary
When a buyer sends more ICP than the per-participant cap (`max_participant_icp_e8s`) or the remaining swap capacity (`available_direct_participation_e8s()`), the `refresh_buyer_token_e8s` function silently accepts only the capped amount and records it in `BuyerState`, but never returns the excess ICP to the buyer. The excess ICP remains stranded in the buyer's subaccount of the swap canister for the entire duration of the OPEN state (up to 90 days). After the swap closes, the excess is not automatically swept back — the buyer must manually call `error_refund_icp` to recover it. A developer-acknowledged TODO comment in the source code confirms this is a known deficiency.

---

### Finding Description

In `rs/sns/swap/src/swap.rs`, the `refresh_buyer_token_e8s` function reads the buyer's subaccount balance on the ICP ledger, then caps the accepted participation at:

```
new_balance_e8s = min(old_amount + requested_increment, max_participant_icp_e8s)
                  further capped by available_direct_participation_e8s()
``` [1](#0-0) 

Only `new_balance_e8s` is stored in `BuyerState`. The function returns successfully without transferring the excess ICP back to the buyer. The developer-acknowledged TODO at line 1133 reads:

```
/// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
``` [2](#0-1) 

The `sweep_icp` function, called during finalization, only transfers `icp_transferable_amount.amount_e8s` (the accepted amount) from the buyer's subaccount — not the full subaccount balance: [3](#0-2) 

This means that after `sweep_icp` runs (whether the swap is COMMITTED or ABORTED), the excess ICP remains in the buyer's subaccount of the swap canister. The only recovery path is `error_refund_icp`: [4](#0-3) 

This function is gated: it is only callable after the swap reaches COMMITTED or ABORTED state, and it is blocked while the buyer's accepted ICP has not yet been disbursed (`transfer_success_timestamp_seconds == 0`): [5](#0-4) 

The test `test_min_max_icp_per_buyer` in `rs/sns/swap/tests/swap.rs` explicitly demonstrates the stranding: a buyer sends 6 ICP, only 5 ICP is accepted (per-participant cap), and the buyer state records only 5 ICP — the 1 ICP excess is silently left in the subaccount: [6](#0-5) 

Similarly, `test_max_icp` shows that when the swap's total cap is reached, a buyer who sent 6 ICP has only 4 ICP accepted, with 2 ICP stranded: [7](#0-6) 

The integration test in `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs` confirms that after a committed swap, users with excess ICP must call `error_refund_icp` to recover it (Case C), and that this is not automatic: [8](#0-7) 

---

### Impact Explanation

Excess ICP is locked in the swap canister's per-buyer subaccounts for the entire OPEN lifecycle (up to 90 days per the swap parameters). After finalization, `sweep_icp` does not sweep the excess — only the accepted amount is moved. Users who do not know to call `error_refund_icp` after the swap closes will have their excess ICP permanently stranded in the swap canister's subaccounts. If the swap canister is eventually deleted (as the protocol allows once "all tokens registered with the swap canister have been disbursed"), any unclaimed excess ICP is permanently lost. [9](#0-8) 

---

### Likelihood Explanation

This condition is triggered whenever a buyer sends more ICP than the per-participant cap or more than the remaining swap capacity. Both scenarios are common in practice: the per-participant cap is a standard swap parameter, and the remaining capacity shrinks as the swap fills up. Any buyer who participates near the end of a filling swap, or who simply sends a round number above the cap, will have excess ICP stranded. The likelihood is **medium** — it requires no special privileges, only a standard `refresh_buyer_tokens` call with an over-funded subaccount.

---

### Recommendation

Modify `refresh_buyer_token_e8s` to automatically transfer excess ICP back to the buyer's account when the accepted amount is less than the subaccount balance. This is already acknowledged in the codebase via `TODO(NNS1-1682)`. The refund should be attempted inline (before returning), using the same ICP ledger transfer mechanism used in `error_refund_icp`, so that users are not required to take a separate manual action. [10](#0-9) 

---

### Proof of Concept

1. A swap is OPEN with `max_participant_icp_e8s = 5 ICP` and `max_direct_participation_icp_e8s = 10 ICP`.
2. User1 transfers 6 ICP to their subaccount of the swap canister on the ICP ledger.
3. User1 calls `refresh_buyer_tokens`.
4. `refresh_buyer_token_e8s` reads the balance (6 ICP), caps at `max_participant_icp_e8s` (5 ICP), records 5 ICP in `BuyerState`, and returns `Ok`. No refund is issued.
5. The 1 ICP excess remains in User1's subaccount of the swap canister. User1 cannot recover it while the swap is OPEN.
6. The swap commits. `sweep_icp` transfers 5 ICP (minus fee) from User1's subaccount to SNS governance. The 1 ICP excess remains.
7. User1 must now call `error_refund_icp` with their own principal to recover the 1 ICP. If they do not, the 1 ICP is permanently stranded.

This is directly confirmed by the unit test at `rs/sns/swap/tests/swap.rs:431–486` and the integration test at `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs:890–900`. [11](#0-10)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1133-1134)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1223-1237)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L1950-1960)
```rust
        if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
            if let Some(transfer) = &buyer_state.icp
                && transfer.transfer_success_timestamp_seconds == 0
            {
                // This buyer has ICP not yet disbursed using the normal mechanism.
                return ErrorRefundIcpResponse::new_precondition_error(format!(
                    "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
                    source_principal_id,
                    buyer_state.amount_icp_e8s()
                ));
            }
```

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

**File:** rs/sns/swap/src/swap.rs (L2096-2121)
```rust
            let icp_transferable_amount = match buyer_state.icp.as_mut() {
                Some(transferable_amount) => transferable_amount,
                // BuyerState.icp should always be present as it is set in `refresh_buyer_tokens`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "PrincipalId {} has corrupted BuyerState: {:?}",
                        principal,
                        buyer_state
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };

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

**File:** rs/sns/swap/tests/swap.rs (L431-486)
```rust
    // Try to deposit 6 ICP.
    {
        let e = swap
            .refresh_buyer_token_e8s(
                *TEST_USER1_PRINCIPAL,
                None,
                SWAP_CANISTER_ID,
                &mock_stub(vec![LedgerExpect::AccountBalance(
                    Account {
                        owner: SWAP_CANISTER_ID.get().into(),
                        subaccount: Some(principal_to_subaccount(&TEST_USER1_PRINCIPAL.clone())),
                    },
                    Ok(Tokens::from_e8s(6 * E8)),
                )]),
            )
            .now_or_never()
            .unwrap();
        assert!(e.is_ok());
        // Should only get 5 as that's the max per participant.
        assert_eq!(
            swap.buyers
                .get(&TEST_USER1_PRINCIPAL.to_string())
                .unwrap()
                .amount_icp_e8s(),
            5 * E8
        );
        // Make sure that a second refresh of the same principal doesn't change the balance.
        let e = swap
            .refresh_buyer_token_e8s(
                *TEST_USER1_PRINCIPAL,
                None,
                SWAP_CANISTER_ID,
                &mock_stub(vec![LedgerExpect::AccountBalance(
                    Account {
                        owner: SWAP_CANISTER_ID.get().into(),
                        subaccount: Some(principal_to_subaccount(&TEST_USER1_PRINCIPAL.clone())),
                    },
                    Ok(Tokens::from_e8s(10 * E8)),
                )]),
            )
            .now_or_never()
            .unwrap();
        assert!(e.is_ok());
        // Should still only be 5 as that's the max per participant.
        assert_eq!(
            swap.buyers
                .get(&TEST_USER1_PRINCIPAL.to_string())
                .unwrap()
                .amount_icp_e8s(),
            5 * E8
        );

        // Assert that the buyer list was updated in order
        let buyers_list = get_snapshot_of_buyers_index_list();
        assert_eq!(vec![*TEST_USER1_PRINCIPAL,], buyers_list);
    }
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

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L890-900)
```rust
        } else {
            // Case C: Expecting to get refunded with Transferred - Accepted - (ICP Ledger transfer fee).
            assert_matches!(
                error_refund_icp_result,
                error_refund_icp_response::Result::Ok(_)
            );

            attempted_participation_amount_e8s
                - accepted_participation_amount_e8s
                - DEFAULT_TRANSFER_FEE.get_e8s()
        };
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L149-150)
```text
// The `swap` canister can be deleted when all tokens registered with the
// `swap` canister have been disbursed to their rightful owners.
```
