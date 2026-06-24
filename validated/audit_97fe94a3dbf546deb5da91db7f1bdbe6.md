Audit Report

## Title
Excess ICP Locked in SNS Swap Canister Subaccount Without Automatic Refund - (`rs/sns/swap/src/swap.rs`)

## Summary

`refresh_buyer_token_e8s` reads the full ICP balance from a buyer's subaccount but records only the capped amount (`min(available_direct_participation_e8s, max_participant_icp_e8s)`) in `self.buyers`. The excess ICP is never returned within the function and cannot be recovered via `error_refund_icp` while the swap is OPEN. The developers explicitly acknowledge this gap with `TODO(NNS1-1682)`. Any buyer who deposits more than either cap has their excess ICP locked in the swap canister's subaccount for the entire swap duration, recoverable only by a manual post-close call to `error_refund_icp`.

## Finding Description

**Root cause:** In `refresh_buyer_token_e8s` (`rs/sns/swap/src/swap.rs`), the function reads the full subaccount balance `e8s` from the ICP ledger, then computes a capped `new_balance_e8s`:

```
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

Only `new_balance_e8s` is stored in `self.buyers`. The difference `e8s - new_balance_e8s` remains in the subaccount with no transfer back to the buyer initiated inside this function. The developer-acknowledged TODO at line 1133 confirms this is an unresolved gap. [1](#0-0) [2](#0-1) 

**Why existing checks fail:**

1. `error_refund_icp` is gated behind a lifecycle check — it returns a `PRECONDITION` error if the swap is not ABORTED or COMMITTED, so the excess is inaccessible for the entire OPEN duration. [3](#0-2) 

2. `sweep_icp` iterates over `self.buyers` and calls `transfer_helper` using `icp_transferable_amount.amount_e8s` — the capped recorded value — not the actual subaccount balance. The excess ICP in the subaccount is not swept. [4](#0-3) [5](#0-4) 

3. Integration tests confirm the behavior: depositing 6 ICP against a 5 ICP per-participant cap results in only 5 ICP recorded, with the remaining 1 ICP silently left in the subaccount. [6](#0-5) 

**Exploit path:**
- Precondition: Swap is OPEN with `max_participant_icp_e8s = N` or remaining pool capacity `< deposit`.
- Attacker/user action: Transfer `N + delta` ICP to `subaccount(swap_canister, buyer_principal)` on the ICP ledger, then call `refresh_buyer_tokens`.
- Trigger: `refresh_buyer_token_e8s` records only `N`, leaving `delta` locked.
- Bad result: `delta` ICP is inaccessible for the entire swap duration. After the swap closes, the user must explicitly call `error_refund_icp` to recover it (minus an additional transfer fee). If the user never calls it, the ICP remains stranded indefinitely. [7](#0-6) 

## Impact Explanation

Any unprivileged buyer who deposits more than `max_participant_icp_e8s` or more than the remaining pool capacity has their excess ICP locked in the swap canister's subaccount for the full swap duration (potentially days to weeks). The excess is not automatically returned by `sweep_icp` or `finalize`. If the user does not call `error_refund_icp` post-close, the ICP remains stranded indefinitely. This constitutes a significant SNS security impact with concrete user fund harm, matching the **High** impact class: "Significant SNS... security impact with concrete user or protocol harm."

## Likelihood Explanation

This is triggered by normal, unprivileged user behavior — sending a round number that exceeds the per-participant cap, or participating when the pool is nearly full. No special privileges, technical skill, or attack setup is required. The `refresh_buyer_tokens` endpoint is a public update method callable by any principal. The `TODO(NNS1-1682)` comment confirms the developers are aware this is an unresolved gap. The scenario is highly likely in any real SNS swap. [8](#0-7) 

## Recommendation

Inside `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, calculate the excess (`e8s - new_balance_e8s`) and immediately initiate an ICP ledger transfer back to the buyer's principal account for that excess amount. This should be done atomically within the same call, before returning, rather than deferring to a separate post-close `error_refund_icp` call. The transfer fee for the refund should be deducted from the excess. If the excess is less than the transfer fee, it can be left in the subaccount (as it would be uneconomical to refund).

## Proof of Concept

The existing unit test at `rs/sns/swap/tests/swap.rs` lines 431–486 already demonstrates the behavior:

1. Configure swap with `max_participant_icp_e8s = 5 * E8`.
2. Mock ledger returns balance of `6 * E8` for the buyer's subaccount.
3. Call `refresh_buyer_token_e8s`.
4. Assert `buyers[TEST_USER1_PRINCIPAL].amount_icp_e8s() == 5 * E8` — confirmed by test.
5. The remaining `1 * E8` stays in the subaccount; no refund transfer is initiated.
6. Calling `error_refund_icp` while swap is OPEN returns a `PRECONDITION` error.
7. Only after swap closes (COMMITTED or ABORTED) can the user recover the excess via `error_refund_icp`. [6](#0-5)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1133-1134)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1224-1237)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1931-1936)
```rust
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
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

**File:** rs/sns/swap/src/types.rs (L612-627)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
        if self.transfer_start_timestamp_seconds > 0 {
            // Operation in progress...
            return TransferResult::AlreadyStarted;
        }
        self.transfer_start_timestamp_seconds = now_fn(false);

        // The ICRC1Ledger Trait converts any errors to Err(NervousSystemError).
        // No panics should occur when issuing this transfer.
        let result = ledger
            .transfer_funds(
                amount.get_e8s().saturating_sub(fee.get_e8s()),
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

**File:** rs/sns/swap/canister/canister.rs (L127-143)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
}
```
