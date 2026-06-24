### Title
Guard Exhaustion DoS on ckETH/ckERC20 Withdrawals via 100 Concurrent `withdraw_erc20` Calls — (`rs/ethereum/cketh/minter/src/guard/mod.rs`, `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

`withdraw_erc20` acquires a `PendingWithdrawalRequests` guard before any async work and holds it across up to three sequential `await` points (gas fee estimation + two inter-canister ledger burns). Because `MAX_CONCURRENT = 100` equals the exact number of distinct principals needed to fill `pending_withdrawal_principals`, 100 unprivileged principals can exhaust the guard set and cause every subsequent call to `withdraw_eth` or `withdraw_erc20` to `ic_cdk::trap`, blocking all new withdrawals for the duration of the in-flight calls.

---

### Finding Description

**Guard constants and shared set**

`MAX_CONCURRENT` is set to `100` and both `withdraw_eth` and `withdraw_erc20` share the same guard type (`PendingWithdrawalRequests`) backed by `state.pending_withdrawal_principals`. [1](#0-0) [2](#0-1) 

**Guard acquisition and trap on failure**

`withdraw_erc20` acquires the guard at the very top of the function, before any await, and calls `ic_cdk::trap` (hard abort, not a graceful `Err`) if the guard cannot be obtained: [3](#0-2) 

`withdraw_eth` does the same: [4](#0-3) 

**Guard held across three await points in `withdraw_erc20`**

After guard acquisition, `withdraw_erc20` suspends at:

1. `estimate_erc20_transaction_fee().await` → `lazy_refresh_gas_fee_estimate().await` (may involve an inter-canister call to refresh gas price)
2. `cketh_ledger.burn_from(...).await` — inter-canister call to the ckETH ledger
3. `ckerc20_ledger.burn_from(...).await` — inter-canister call to the ckERC20 ledger [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

The guard is only released when `_guard` drops at function exit: [9](#0-8) 

**Guard limit check**

The check that triggers the trap path: [10](#0-9) 

**`withdraw_eth` holds the guard across only one await point** (one ledger burn), making `withdraw_erc20` the more effective exhaustion vector because the guard is held for the combined latency of two sequential inter-canister calls. [11](#0-10) 

---

### Impact Explanation

When `pending_withdrawal_principals.len() == 100`, any new call to `withdraw_eth` or `withdraw_erc20` from a principal not already in the set hits `TooManyConcurrentRequests` and is immediately trapped. No new ckETH or ckERC20 withdrawal can be initiated until at least one of the 100 in-flight calls completes. The window is bounded by the combined latency of two sequential ledger inter-canister calls per in-flight message, which under normal IC conditions is on the order of seconds to tens of seconds per call — but an attacker who controls the timing of their own calls (e.g., by ensuring the ckERC20 burn is the slow step) can extend the window.

---

### Likelihood Explanation

- Requires 100 distinct non-anonymous principals (cheap on the IC).
- Each principal must hold the guard long enough for all 100 to be simultaneously in-flight. Because the IC scheduler interleaves messages at every `await` point, this is the normal execution model — not a race condition.
- If `lazy_refresh_gas_fee_estimate` makes an inter-canister call, the guard is held even before any balance check, meaning **no real funds are required** to hold the guard during that phase.
- If gas fee estimation is a local read, the attacker needs valid ckETH balances for 100 principals to hold the guard past the first burn — an economic cost but not a prohibitive one for a motivated attacker targeting a high-value bridge.
- The `AlreadyProcessing` check prevents a single principal from holding multiple guards, so the attacker genuinely needs 100 distinct principals. [12](#0-11) 

---

### Recommendation

1. **Decouple guard scope from ledger latency**: Release the guard immediately after the withdrawal request is durably recorded in state (after both burns succeed), rather than holding it across all inter-canister calls. The guard's purpose is reentrancy prevention, not queue admission control.
2. **Raise `MAX_CONCURRENT` or make it asymmetric**: `MAX_CONCURRENT = 100` is simultaneously the concurrency limit and the exact exhaustion threshold. A value significantly higher than realistic peak concurrency (e.g., 1000) would make exhaustion impractical.
3. **Return `Err(...)` instead of `ic_cdk::trap` on `TooManyConcurrentRequests`**: Trapping is appropriate for programming errors, not for expected resource-limit conditions. Returning a typed error allows callers to retry gracefully.
4. **Separate the guard sets for `withdraw_eth` and `withdraw_erc20`**: Currently both share `pending_withdrawal_principals`, so `withdraw_erc20` exhaustion also blocks `withdraw_eth`.

---

### Proof of Concept

```
State-machine test outline:
1. Initialize minter with ckETH + one ckERC20 token.
2. Create 100 principals, each with sufficient ckETH and ckERC20 allowances.
3. Send 100 concurrent `withdraw_erc20` ingress messages (one per principal).
4. Tick the IC scheduler so each message reaches its first await (gas fee estimation or ckETH burn) but does NOT complete — mock ledger holds responses.
5. Assert: `read_state(|s| s.pending_withdrawal_principals.len()) == 100`.
6. Send a 101st `withdraw_erc20` from a fresh principal.
7. Assert: the 101st call traps with a message containing "TooManyConcurrentRequests".
8. Also assert: a `withdraw_eth` call from the same fresh principal also traps.
9. Release the mock ledger responses; assert all 100 guards are dropped and a new call succeeds.
```

### Citations

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L27-34)
```rust
impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn guarded_principals(state: &mut State) -> &mut BTreeSet<Principal> {
        &mut state.pending_withdrawal_principals
    }

    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L56-58)
```rust
            if principals.contains(&principal) {
                return Err(GuardError::AlreadyProcessing);
            }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L59-61)
```rust
            if principals.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L71-74)
```rust
impl<PR: RequestsGuardedByPrincipal> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::guarded_principals(s).remove(&self.principal));
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L274-278)
```rust
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-312)
```rust
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L401-405)
```rust
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L468-477)
```rust
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```
