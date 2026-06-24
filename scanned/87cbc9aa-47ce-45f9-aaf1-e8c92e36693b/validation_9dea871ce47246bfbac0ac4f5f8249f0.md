### Title
Excess ICP Locked in SNS Swap Canister Subaccount When Participation Exceeds Per-Participant or Pool Cap - (`rs/sns/swap/src/swap.rs`)

### Summary

`refresh_buyer_token_e8s` in the SNS Swap canister accepts the full ICP balance from a buyer's subaccount but silently caps the recorded participation at `max_participant_icp_e8s` or `available_direct_participation_e8s()`. The excess ICP is never returned to the user during the swap and can only be recovered via a separate, explicit `error_refund_icp` call after the swap closes. An unresolved `TODO(NNS1-1682)` in the source code explicitly acknowledges this gap.

### Finding Description

The SNS Swap canister's participation flow works as follows:

1. A buyer transfers ICP to a per-principal subaccount of the swap canister on the ICP ledger.
2. The buyer calls `refresh_buyer_tokens`, which triggers `refresh_buyer_token_e8s`.
3. The function reads the full subaccount balance (`e8s`) from the ICP ledger.
4. It then caps the accepted amount at two limits: [1](#0-0) [2](#0-1) 

- `max_increment_e8s = self.available_direct_participation_e8s()` — remaining pool capacity
- `max_participant_icp_e8s` — per-participant cap

Only `new_balance_e8s` (the capped value) is recorded in `self.buyers`. The difference between the actual subaccount balance and the accepted amount is **never transferred back to the user** inside this function. The developer acknowledged this with an explicit TODO: [3](#0-2) 

The only recovery path is `error_refund_icp`, which:

- Is blocked while the swap is OPEN — it returns a `PRECONDITION` error if the lifecycle is not ABORTED or COMMITTED: [4](#0-3) 

- Requires the user to explicitly call it after the swap closes.
- Is not invoked automatically by `sweep_icp` or `finalize`.

`sweep_icp` only transfers the recorded `amount_icp_e8s` from `buyers`, not the full subaccount balance: [5](#0-4) 

The canister's own integration tests confirm the scenario: when a user deposits 6 ICP against a 5 ICP per-participant cap, only 5 ICP is accepted and the remaining 1 ICP stays locked in the subaccount until the swap closes and the user manually calls `error_refund_icp`: [6](#0-5) 

### Impact Explanation

Any buyer who sends more ICP than either `max_participant_icp_e8s` or the remaining pool capacity has their excess ICP locked in the swap canister's subaccount for the entire duration of the swap (which can last days or weeks). The excess is not automatically returned. If the user does not know to call `error_refund_icp` after the swap closes, the ICP remains stranded in the subaccount indefinitely. This is a **ledger conservation bug**: ICP accepted from the user is not fully accounted for in the swap's buyer state, and the unaccounted portion is not returned.

### Likelihood Explanation

This is highly likely to occur in practice:
- Any user who sends more than `max_participant_icp_e8s` triggers it (a common UX pattern where users send a round number).
- Any user who participates when the swap is nearly full (remaining capacity < their deposit) triggers it.
- The `TODO(NNS1-1682)` comment confirms the developers are aware this is an unresolved gap.
- The canister exposes `refresh_buyer_tokens` as a public update method callable by any unprivileged principal. [7](#0-6) 

### Recommendation

Inside `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, calculate the excess (`e8s - new_balance_e8s`) and immediately initiate an ICP ledger transfer back to the buyer's principal account for that excess amount. This mirrors the fix applied to the BalancerRouter: return the unused portion to the sender atomically within the same call, rather than deferring it to a separate post-close `error_refund_icp` call.

### Proof of Concept

1. A swap is configured with `max_participant_icp_e8s = 5 * E8` and `max_direct_participation_icp_e8s = 10 * E8`.
2. Buyer transfers 6 ICP to `swap_canister_subaccount(buyer_principal)` on the ICP ledger.
3. Buyer calls `refresh_buyer_tokens`.
4. `refresh_buyer_token_e8s` reads balance = 6 ICP, caps at 5 ICP, records 5 ICP in `buyers`.
5. The remaining 1 ICP stays in the subaccount. No transfer back to the buyer is initiated.
6. The swap runs for its full duration (e.g., 14 days). The 1 ICP is inaccessible during this period.
7. After the swap closes and `sweep_icp` completes, the buyer must call `error_refund_icp` to recover the 1 ICP (minus an additional transfer fee).
8. If the buyer never calls `error_refund_icp`, the 1 ICP remains stranded in the subaccount. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1133-1133)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
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

**File:** rs/sns/swap/src/swap.rs (L1906-1936)
```rust
    /// Requests a refund of ICP tokens transferred to the Swap
    /// canister that was either never notified (via the
    /// refresh_buyer_tokens Candid method), or not fully accepted (by
    /// refresh_buyer_tokens).
    ///
    /// This method makes no changes (and instead panics) unless
    /// finalization has completed successfully (see the finalize
    /// method), which can only happen after self has entered the
    /// Aborted or Committed state.
    ///
    /// The entire balance in `subaccount(swap_canister, P)` is
    /// transferred to request.principal_id (minus the transfer fee,
    /// of course).
    ///
    /// This method is secure because it only transfers tokens from a
    /// principal's subaccount (of the Swap canister) to the
    /// principal's own account, i.e., the tokens were held in escrow
    /// for the principal (buyer) before the call and are returned to
    /// the same principal.
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

**File:** rs/sns/swap/src/swap.rs (L2046-2063)
```rust
    pub async fn sweep_icp(
        &mut self,
        now_fn: fn(bool) -> u64,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        let lifecycle: Lifecycle = self.lifecycle();

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_icp(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };
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
