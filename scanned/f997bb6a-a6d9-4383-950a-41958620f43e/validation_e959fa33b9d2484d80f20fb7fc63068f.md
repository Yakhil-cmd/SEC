### Title
Missing Slippage Protection in SNS Swap `refresh_buyer_tokens` Allows Silent ICP Contribution Reduction - (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS swap canister's `refresh_buyer_token_e8s` function silently caps a user's ICP contribution at the remaining swap capacity (`available_direct_participation_e8s()`) without allowing users to specify a minimum accepted participation amount. Because participation requires a two-step process (transfer ICP to subaccount, then call `refresh_buyer_tokens`), a race condition exists where a user's ICP is locked in the swap subaccount with a smaller-than-expected contribution — or no contribution at all — with no recourse until the swap closes.

### Finding Description
The `RefreshBuyerTokensRequest` message contains only `buyer` and `confirmation_text` fields; there is no `min_icp_accepted_participation_e8s` field. [1](#0-0) 

Inside `refresh_buyer_token_e8s`, after awaiting the ICP ledger balance, the accepted increment is silently clamped to whatever capacity remains: [2](#0-1) [3](#0-2) 

The user receives `icp_accepted_participation_e8s` in the response, but had no way to express "accept my full amount or reject the call entirely." [4](#0-3) 

The two-step participation flow is:
1. User transfers ICP to their personal swap subaccount on the ICP ledger (irreversible).
2. User calls `refresh_buyer_tokens` to register the contribution.

Between steps 1 and 2, any other participant can call `refresh_buyer_tokens` and consume the remaining capacity. The original user's call then either:
- **Fails** (if the swap is now completely full and `validate_possibility_of_direct_participation` rejects it), leaving the ICP locked in the subaccount; or
- **Succeeds with a reduced amount** (if partial capacity remains), silently accepting less ICP than the user intended, with the excess locked in the subaccount.

In both cases the user cannot recover their ICP until the swap closes (committed or aborted) via `error_refund_icp`, which can take days or weeks. [5](#0-4) 

### Impact Explanation
A user who transfers ICP expecting to contribute a specific amount may end up with a materially smaller contribution (or zero contribution) while their ICP is locked for the duration of the swap. There is no mechanism to express a minimum acceptable participation amount, so the user cannot protect themselves from this outcome. The locked ICP represents a real opportunity cost and a loss of user funds for the duration of the swap lifecycle.

**Impact: Medium** — funds are temporarily locked (not permanently lost), but the user's intended participation is silently altered without consent.

### Likelihood Explanation
Popular SNS swaps routinely fill up within seconds of opening. The mandatory two-step participation flow (ledger transfer → `refresh_buyer_tokens`) creates a natural race window that any concurrent participant can exploit without any special privilege. No privileged access, key material, or network-level attack is required — any unprivileged ingress sender can call `refresh_buyer_tokens` and consume remaining capacity.

**Likelihood: Medium** — the race window exists in every swap and is most impactful near the `max_direct_participation_icp_e8s` cap.

### Recommendation
Add an optional `min_icp_accepted_participation_e8s` field to `RefreshBuyerTokensRequest`:

```protobuf
message RefreshBuyerTokensRequest {
  string buyer = 1;
  optional string confirmation_text = 2;
  optional uint64 min_icp_accepted_participation_e8s = 3; // NEW
}
```

In `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, check:

```rust
if let Some(min_accepted) = min_icp_accepted_participation_e8s {
    let actual_increment = new_balance_e8s.saturating_sub(old_amount_icp_e8s);
    if actual_increment < min_accepted {
        return Err(format!(
            "Slippage: accepted increment {} is less than minimum required {}",
            actual_increment, min_accepted
        ));
    }
}
```

This allows users to atomically express their intent and receive a clear error instead of a silent partial fill, analogous to `min_amount_out` in DeFi swap protocols.

### Proof of Concept

1. A popular SNS swap opens with `max_direct_participation_icp_e8s = 500_000 ICP` and 100 ICP remaining capacity.
2. Alice observes 100 ICP remaining and transfers exactly 100 ICP to her swap subaccount on the ICP ledger.
3. Before Alice calls `refresh_buyer_tokens`, Bob calls `refresh_buyer_tokens` and fills the remaining 100 ICP capacity. The swap is now at its cap.
4. Alice calls `refresh_buyer_tokens`. Inside `refresh_buyer_token_e8s`:
   - `available_direct_participation_e8s()` returns 0.
   - `actual_increment_e8s = min(0, 100 ICP) = 0`.
   - `new_balance_e8s = 0`, which is below `min_participant_icp_e8s`, so the call returns an error.
5. Alice's 100 ICP is now locked in the swap subaccount. She cannot retrieve it until the swap closes via `error_refund_icp`. [6](#0-5)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-855)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
message RefreshBuyerTokensResponse {
  uint64 icp_accepted_participation_e8s = 1;
  uint64 icp_ledger_account_balance_e8s = 2;
}
```

**File:** rs/sns/swap/src/swap.rs (L1113-1132)
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
```

**File:** rs/sns/swap/src/swap.rs (L1134-1141)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1200-1246)
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

        // Check that the new_balance_e8s is bigger than or equal to the minimum required for
        // participating.
        if new_balance_e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Rejecting participation of effective amount {}; minimum required to participate: {}",
                new_balance_e8s, params.min_participant_icp_e8s
            ));
        }
```
