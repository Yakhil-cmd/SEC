### Title
Lack of Automatic Refund for Excess ICP Participation in SNS Swap - (`rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister's `refresh_buyer_token_e8s` function accepts ICP participation up to `max_participant_icp_e8s` but does not automatically refund the excess ICP that a buyer may have transferred beyond that cap. The excess remains locked in the swap canister's per-buyer subaccount until the swap closes, at which point the user must manually invoke `error_refund_icp` to recover it. If the user is unaware of this mechanism, the excess ICP is permanently lost. A developer-acknowledged TODO in the production code confirms this is an unresolved gap.

### Finding Description
In `rs/sns/swap/src/swap.rs`, the `refresh_buyer_token_e8s` function reads the full ICP balance of the buyer's subaccount on the swap canister, then caps the accepted increment at `min(max_increment_e8s, requested_increment_e8s)` and further caps the running total at `max_participant_icp_e8s`.

```rust
// rs/sns/swap/src/swap.rs ~L1222-1237
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
...
let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

The function records only the capped amount in `self.buyers` and returns successfully, but the full transferred balance (including the excess) remains in the buyer's subaccount on the ICP ledger under the swap canister's control. No transfer back to the buyer is attempted. The code itself carries an explicit unresolved TODO:

```rust
/// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
pub async fn refresh_buyer_token_e8s(...)
``` [1](#0-0) [2](#0-1) 

The only recovery path is `error_refund_icp`, which is callable only after the swap has reached a terminal lifecycle state (Committed or Aborted). During the entire open period of the swap, the excess ICP is inaccessible to the buyer.

### Impact Explanation
Any buyer who transfers more ICP than `max_participant_icp_e8s` (or more than the remaining `available_direct_participation_e8s`) has their excess ICP locked in the swap canister for the duration of the swap. If the buyer does not subsequently call `error_refund_icp` after the swap closes, the excess ICP is permanently unrecoverable. The swap canister accumulates these excess balances across all over-contributing buyers with no on-chain mechanism to return them proactively. This is a direct ledger conservation issue: ICP is transferred to the swap canister but neither accepted into the swap nor returned to the sender.

**Impact: 4** — Real ICP loss for users who overpay and do not know to call `error_refund_icp` post-swap. [3](#0-2) 

### Likelihood Explanation
The scenario is reachable by any unprivileged ingress sender. A buyer who does not know the exact remaining capacity of the swap (which changes dynamically as other buyers participate) will routinely transfer more than the cap. The `test_min_max_icp_per_buyer` test explicitly demonstrates that depositing 6 ICP when the cap is 5 ICP results in only 5 ICP being accepted, with no refund issued.

**Likelihood: 3** — Common user error; the swap capacity is dynamic and not easily predictable at transfer time. [4](#0-3) 

### Recommendation
Inside `refresh_buyer_token_e8s`, after computing `actual_increment_e8s`, calculate the unaccepted excess (`e8s - old_amount_icp_e8s - actual_increment_e8s`) and immediately transfer it back to the buyer's principal on the ICP ledger before returning. This mirrors the fix applied in the referenced ERC20 bridge report and resolves the open `TODO(NNS1-1682)`.

### Proof of Concept

**Entry path:** Any principal calls `refresh_buyer_tokens` (the public canister endpoint wrapping `refresh_buyer_token_e8s`) after transferring ICP to their buyer subaccount on the swap canister. [5](#0-4) 

**Concrete scenario (mirroring the existing unit test):**

1. Swap is open with `max_participant_icp_e8s = 5 * E8`.
2. Buyer transfers `6 * E8` ICP to their subaccount (`swap_canister_id / principal_to_subaccount(buyer)`).
3. Buyer calls `refresh_buyer_tokens`.
4. `refresh_buyer_token_e8s` reads balance = `6 * E8`, caps accepted amount at `5 * E8`, records `5 * E8` in `self.buyers`, and returns `Ok`.
5. The remaining `1 * E8` ICP stays in the buyer's subaccount under the swap canister's control.
6. No refund is issued. The buyer's `1 * E8` ICP is inaccessible until the swap closes and the buyer manually calls `error_refund_icp`. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1113-1134)
```rust
    /// In state Open, this method can be called to refresh the amount
    /// of ICP a buyer has contributed from the ICP ledger canister.
    ///
    /// It is assumed that prior to calling this method, tokens have
    /// been transferred by the buyer to a subaccount of the swap
    /// canister (this canister) on the ICP ledger.
    /// Also, deletes an existing ticket if it has been fully executed
    /// (i.e. the requested increment is >= that the ticket amount).
    /// (This allows participation to be increased later.)
    ///
    /// If the SNS had specified a swap confirmation text, the caller of this
    /// function must accept this confirmation by sending the exact same text
    /// as an argument to this function (otherwise, the call will result in
    /// an error).
    ///
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1200-1237)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
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
