### Title
SNS Swap `refresh_buyer_token_e8s` Accepts Less ICP Than Transferred Without Immediate Refund, Locking User Funds - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The `refresh_buyer_token_e8s` function in the SNS Swap canister contains a TOCTOU (Time-of-Check-Time-of-Use) race condition across an inter-canister `await` point. A concurrent participant can consume the remaining swap capacity while Alice's async ledger balance call is in-flight, causing Alice's accepted ICP amount to be far less than what she transferred. The excess ICP is locked in Alice's subaccount of the swap canister until the swap closes (COMMITTED or ABORTED), with no immediate refund. The code itself acknowledges this with `// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.`

---

### Finding Description

The two-step participation flow requires a buyer to:
1. Transfer ICP to their personal subaccount of the swap canister on the ICP ledger.
2. Call `refresh_buyer_token_e8s` to register the participation.

Inside `refresh_buyer_token_e8s`, the function is `async` and makes an inter-canister call to the ICP ledger to read the buyer's subaccount balance: [1](#0-0) 

This `await` is a yield point. While Alice's call is suspended waiting for the ledger response, Bob can submit his own `refresh_buyer_token_e8s` call, which runs to completion and consumes most of the remaining swap capacity. When Alice's call resumes, `available_direct_participation_e8s()` is recomputed from the now-updated state: [2](#0-1) 

The accepted increment is then capped to whatever tiny capacity remains: [3](#0-2) 

Alice's `BuyerState` is updated with the tiny accepted amount, but the full transferred balance remains locked in her subaccount of the swap canister. The function explicitly documents that no immediate refund is attempted: [4](#0-3) 

The only recovery path is `error_refund_icp`, which is gated behind the swap reaching COMMITTED or ABORTED state: [5](#0-4) 

The `BuyerState` invariant documented in the protobuf schema confirms the design intent but does not prevent the mismatch: [6](#0-5) 

---

### Impact Explanation

A buyer who transfers a large amount of ICP (e.g., 500,000 ICP) may have only a tiny fraction accepted (e.g., 1 ICP) due to concurrent front-running. The consequences are:

1. **Capital lockup**: The excess ICP (499,999 ICP) is locked in the swap canister's subaccount for the entire duration of the swap — potentially days or weeks — without earning staking rewards.
2. **Token shortfall**: The buyer receives SNS neurons worth only 1 ICP instead of 500,000 ICP, a near-total loss of intended participation.
3. **No immediate remedy**: The buyer cannot recover the locked ICP until the swap finalizes. During the OPEN state, `error_refund_icp` is rejected.

This is a direct ledger conservation bug: the ICP committed to the swap canister does not correspond to the SNS tokens the buyer will receive.

---

### Likelihood Explanation

SNS decentralization swaps are high-demand, time-limited events where many participants submit transactions concurrently. The inter-canister call to the ICP ledger inside `refresh_buyer_token_e8s` introduces a mandatory yield point that is exploitable by any concurrent participant. No special privileges are required — any unprivileged ingress sender can call `refresh_buyer_token_e8s`. The scenario is especially likely near the end of a swap when remaining capacity is small and competition is highest, exactly mirroring the SlowRoll report's "final wave" scenario.

---

### Recommendation

Implement the already-acknowledged fix referenced by `TODO(NNS1-1682)`: within `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, immediately refund the excess ICP (`e8s - new_balance_e8s`) back to the buyer's principal account on the ICP ledger before returning. This ensures the amount of ICP locked in escrow always equals the accepted participation amount, eliminating the conservation mismatch.

---

### Proof of Concept

**Setup**: SNS swap is OPEN. `max_direct_participation_icp_e8s = 500_000 * E8`. `min_participant_icp_e8s = 1 * E8`. Current participation = `499_999 * E8` (1 ICP remaining capacity).

**Steps**:

1. Alice transfers `500_000 * E8` ICP to `subaccount(swap_canister, Alice)` on the ICP ledger.
2. Alice calls `refresh_buyer_token_e8s`. The function validates lifecycle (OPEN) and calls `icp_ledger.account_balance(Alice's subaccount)` — **await point**.
3. During the await, Bob calls `refresh_buyer_token_e8s` with a balance of `1 * E8` ICP. Bob's call completes synchronously (no interleaving within a single message), consuming the last `1 * E8` of capacity. `available_direct_participation_e8s()` is now `0`.
4. Alice's call resumes. `max_increment_e8s = available_direct_participation_e8s() = 0`.
5. `actual_increment_e8s = min(0, 500_000 * E8) = 0`. `new_balance_e8s = 0`.
6. The check `new_balance_e8s < min_participant_icp_e8s` triggers; Alice's call returns an `Err`.
7. Alice's `500_000 * E8` ICP remains locked in `subaccount(swap_canister, Alice)`.
8. Alice cannot call `error_refund_icp` until the swap closes.

**Variant** (accepted but tiny): If Bob consumed only `499_999 * E8` leaving `1 * E8` capacity, Alice's call succeeds with `icp_accepted_participation_e8s = 1 * E8` while `499_999 * E8` ICP is locked — directly analogous to the SlowRoll overbuying scenario. [7](#0-6) [8](#0-7) [2](#0-1) [3](#0-2) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1128-1133)
```rust
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1134-1134)
```rust
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1152-1163)
```rust
        // Look for the token balance of the specified principal's subaccount on 'this' canister.
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1165-1171)
```rust
        // Recheck lifecycle state and ICP target after async call because the swap could have
        // been closed (committed or aborted) while the call to get the account balance was
        // outstanding.
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1223-1225)
```rust
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1241-1245)
```rust
        if new_balance_e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Rejecting participation of effective amount {}; minimum required to participate: {}",
                new_balance_e8s, params.min_participant_icp_e8s
            ));
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L667-673)
```text
  // Invariant between canisters in the OPEN state:
  //
  //  ```text
  //  icp.amount_e8 <= icp_ledger.balance_of(subaccount(swap_canister, P)),
  //  ```
  //
  // where `P` is the principal ID associated with this buyer's state.
```
