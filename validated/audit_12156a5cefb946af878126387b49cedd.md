### Title
Concurrent `refresh_buyer_tokens` Calls Allow Exceeding Maximum Direct Participation Cap - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The `refresh_buyer_token_e8s` function in the SNS Swap canister computes the available participation capacity (`max_increment_e8s`) **after** an inter-canister `await` point but **before** updating the buyer state. Because there is no per-buyer or global lock protecting this critical section, two concurrent calls for different buyers can both observe the same stale `max_increment_e8s`, and both commit their full requested increment, causing the total accepted ICP to exceed `max_direct_participation_icp_e8s`.

---

### Finding Description

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` follows this sequence:

1. Pre-await lifecycle checks (lines 1144–1150).
2. **Inter-canister `await`**: `icp_ledger.account_balance(account).await` (lines 1158–1162).
3. Post-await lifecycle re-checks (lines 1168–1171).
4. **Compute `max_increment_e8s`** from `self.available_direct_participation_e8s()` (line 1177) — this is the available capacity *at this moment*.
5. Read `old_amount_icp_e8s` from `self.buyers` (lines 1210–1213).
6. Compute `actual_increment_e8s = min(max_increment_e8s, requested_increment_e8s)` (line 1224).
7. **State update**: write `new_balance_e8s` into `self.buyers` and call `update_total_participation_amounts()` (lines 1285–1291). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

There is **no per-buyer lock and no global lock** between steps 2 and 7. On the Internet Computer, a canister processes one message at a time, but it yields control at every `await` point. A second ingress message (or a second canister call) for a *different* buyer can be inducted and begin executing while the first call is suspended at the ledger `await`. When both calls resume, they each independently compute `max_increment_e8s` from the same un-updated state, and both commit their full increment.

The public canister endpoint is:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    ...
}
``` [5](#0-4) 

This endpoint is callable by any unprivileged principal with no special role.

**Concrete interleaving:**

| Step | Call A (buyer1, wants 80 ICP) | Call B (buyer2, wants 80 ICP) | Swap state (`max_direct = 100 ICP`) |
|------|-------------------------------|-------------------------------|--------------------------------------|
| 1 | Enters `refresh_buyer_token_e8s`, awaits ledger | — | total = 0, available = 100 |
| 2 | Suspended at `await` | Enters `refresh_buyer_token_e8s`, awaits ledger | total = 0, available = 100 |
| 3 | Resumes; reads `max_increment_e8s = 100` | Suspended at `await` | total = 0 |
| 4 | Writes buyer1 = 80, total = 80 | Resumes; reads `max_increment_e8s = 100` (stale) | total = 80 |
| 5 | — | Writes buyer2 = 80, total = 160 | **total = 160 > max = 100** |

The `validate_possibility_of_direct_participation` re-check after the await (lines 1168–1171) guards against the swap being *closed* during the await, but it does not prevent two concurrent calls from both seeing the same available capacity before either updates the state. [6](#0-5) 

---

### Impact Explanation

**High.** The swap's core invariant — that total accepted ICP never exceeds `max_direct_participation_icp_e8s` — can be violated. Consequences include:

- The swap commits with more ICP than its configured maximum, diluting SNS token distribution for all participants.
- The `current_direct_participation_e8s` field (used to compute SNS token prices and neuron basket sizes) becomes inconsistent with the actual ledger balances.
- Participants who joined early receive fewer SNS tokens than the swap parameters promised.
- The swap's auto-commit trigger (`try_commit`) may fire at the wrong time or with wrong accounting.

---

### Likelihood Explanation

**High.** Any two users who call `refresh_buyer_tokens` concurrently near the swap's participation cap can trigger this. No special privileges, no admin keys, and no threshold corruption are required — only two simultaneous ingress messages from unprivileged principals. This is a routine occurrence during popular SNS launches where many users participate simultaneously.

---

### Recommendation

Apply one of the following mitigations:

1. **Re-check and cap atomically**: After the await, re-read `available_direct_participation_e8s()` immediately before writing to `self.buyers`, and clamp `actual_increment_e8s` to the *current* available capacity at that exact moment (not the value computed earlier).

2. **Per-buyer lock**: Introduce a set of "in-flight buyer principals" (analogous to the neuron `in_flight_commands` map used in NNS/SNS governance) and reject concurrent calls for the same buyer. For different buyers, the cap re-check in point 1 is still needed.

3. **Global participation lock**: Serialize all `refresh_buyer_tokens` calls with a canister-level flag, similar to `is_finalizing_disburse_maturity` used in SNS governance. [7](#0-6) [8](#0-7) 

---

### Proof of Concept

**Entry path**: Any unprivileged principal can call the `refresh_buyer_tokens` update endpoint on the SNS Swap canister.

**Attack steps**:

1. Attacker (or two coordinated users) each transfer ICP to their respective subaccounts of the swap canister on the ICP ledger.
2. Both call `refresh_buyer_tokens` simultaneously, with amounts that individually fit within the remaining cap but together exceed it.
3. Both calls enter `refresh_buyer_token_e8s` and suspend at `icp_ledger.account_balance(...).await`.
4. Both resume and independently compute `max_increment_e8s = available_direct_participation_e8s()` from the same un-updated state.
5. Both write their full increment to `self.buyers` and call `update_total_participation_amounts()`.
6. The swap's `current_direct_participation_e8s` now exceeds `max_direct_participation_icp_e8s`.

The root cause is the TOCTOU window between the inter-canister `await` at line 1160 and the state mutation at lines 1285–1291, with no lock protecting the critical section. [9](#0-8) [2](#0-1) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1134-1163)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

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

**File:** rs/sns/swap/src/swap.rs (L1210-1224)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L1285-1312)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();

        log!(
            INFO,
            "Refresh_buyer_tokens for buyer {}; old e8s {}; new e8s {}",
            buyer,
            old_amount_icp_e8s,
            new_balance_e8s,
        );
        if new_balance_e8s.saturating_sub(old_amount_icp_e8s) >= max_increment_e8s {
            log!(
                INFO,
                "Swap has reached the direct participation target of {} ICP e8s.",
                self.max_direct_participation_e8s(),
            );
        }

        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: new_balance_e8s,
            icp_ledger_account_balance_e8s: e8s,
        })
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

**File:** rs/sns/governance/src/governance.rs (L4921-4935)
```rust
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
```

**File:** rs/nns/governance/src/neuron_lock.rs (L1-21)
```rust
//! This module defines mechanisms for locking neurons in order to prevent problematic interleaving
//! of neuron operations.
//!
//! The `LedgerUpdateLock` is a legacy mechanism, where the lock contains a `*mut Governance`
//! pointer. An unsafe block is needed to unlock the neuron. In addition, the pointer needs to be
//! `'static` in order for the lock to be used in async contexts. However, using `&'static mut` to
//! access global state is dangerous and should be avoided.
//!
//! The `NeuronAsyncLock` is a new mechanism that uses a `&'static LocalKey<RefCell<Governance>>` to
//! access the global state. This allows for safe access to the global state in async contexts.
//!
//! For sync methods, there is actually no need to acquire the lock, since it's impossible for the
//! lock to be persisted in any case anyway. In the future, a new method on the `Governance` struct
//! can be used to check whether a lock is held for a neuron. However, currently, in order to avoid
//! introducing a 3rd pattern for locking neurons, the recommendation is to keep using
//! `lock_neuron_for_command` with a `SyncCommand`.
//!
//! Note that it's OK for `NeuronAsyncLock` and `LedgerUpdateLock` to co-exist. If a
//! `NeuronAsyncLock` is held for a neuron, and another method tries to acquire a `LedgerUpdateLock`
//! for the same neuron, it will still fail as expected, and vice versa, since their underlying
//! storage is the same `in_flight_commands` map.
```
