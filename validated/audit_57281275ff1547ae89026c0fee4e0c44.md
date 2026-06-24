Audit Report

## Title
Global Pending-Withdrawal Queue Cap With No Per-Principal Sub-Limit Enables Repeatable DoS on All ckETH/ckERC20 Withdrawals - (File: `rs/ethereum/cketh/minter/src/guard/mod.rs`)

## Summary
The ckETH minter enforces a single global cap of `MAX_PENDING = 100` on pending withdrawal requests with no per-principal sub-limit. An attacker controlling 100 distinct IC principals can saturate this queue entirely, causing every subsequent `withdraw_eth` or `withdraw_erc20` call from any user to `ic_cdk::trap` (hard canister error) until the queue drains. Because the attacker's ETH is returned after each Ethereum confirmation cycle, the attack is repeatable indefinitely at the cost of gas fees alone.

## Finding Description

**Root cause — `rs/ethereum/cketh/minter/src/guard/mod.rs`**

`MAX_PENDING` is a single global constant of 100: [1](#0-0) 

`pending_requests_count` returns the total length of the shared queue with no per-principal breakdown: [2](#0-1) 

`Guard::new` checks only this global count: [3](#0-2) 

The guard is dropped (principal removed from `pending_withdrawal_principals`) as soon as the async withdrawal function completes — meaning after a successful call the principal slot is freed but the request remains in `pending_withdrawal_requests`. An attacker using 100 distinct principals can each submit one request sequentially, filling the queue while leaving `pending_withdrawal_principals` empty.

**`withdrawal_requests_len()` is a raw global count:** [4](#0-3) 

**Trap on guard failure — `rs/ethereum/cketh/minter/src/main.rs`**

Both public withdrawal endpoints call `ic_cdk::trap` (not `return Err(...)`) when the guard is denied: [5](#0-4) [6](#0-5) 

The burn happens *after* the guard is acquired, so user funds are not lost — but the withdrawal is completely blocked with a hard `CANISTER_ERROR`.

**Shared queue covers both ckETH and ckERC20:** [7](#0-6) 

**Slow drain — `rs/ethereum/cketh/minter/src/withdraw.rs`**

The minter processes only 5 requests per timer tick: [8](#0-7) 

Each batch requires threshold ECDSA signing and Ethereum on-chain confirmation (~12 minutes), during which the attacker can re-submit to keep the queue saturated.

## Impact Explanation

This is a concrete **application/platform-level DoS** on all ckETH and ckERC20 withdrawals — an explicitly listed High-severity impact class ($2,000–$10,000). Every `withdraw_eth` and `withdraw_erc20` call from any legitimate user receives a hard `CANISTER_ERROR` rejection while the queue is saturated. The attack is repeatable indefinitely, causing sustained unavailability of the withdrawal function for all users of the ckETH/ckERC20 system.

## Likelihood Explanation

No privileged access is required. Any unprivileged IC user can generate 100 key pairs trivially. The capital requirement is approximately 3 ETH (~100 × minimum withdrawal amount of 0.03 ETH), which is fully recovered after each Ethereum confirmation cycle. The only sustained cost is Ethereum gas fees per cycle. The attack is repeatable: as the minter drains 5 requests per tick, the attacker re-submits to maintain saturation. There are no on-chain complexities — only standard IC ingress calls to `withdraw_eth`.

## Recommendation

1. **Add a per-principal pending-request cap** inside `Guard::new`: count how many entries in `pending_withdrawal_requests` belong to the calling principal and reject if it exceeds a small threshold (e.g., 3).
2. **Return a graceful error instead of trapping** when `TooManyPendingRequests` is hit, so callers receive a retryable `TemporarilyUnavailable` response rather than a hard `CANISTER_ERROR`.
3. **Raise `MAX_PENDING`** to a value that makes queue saturation economically prohibitive, or tie it to a per-principal sub-quota.
4. **Separate ckETH and ckERC20 pending counters** so flooding one token type cannot block the other.

## Proof of Concept

1. Generate 100 IC key pairs → 100 distinct principals `P_1 … P_100`.
2. Fund each principal with ≥ `cketh_minimum_withdrawal_amount` ckETH via the ckETH ledger.
3. Call `withdraw_eth` from each principal sequentially with the minimum amount to a valid Ethereum address. Each call: acquires the guard (principal added to `pending_withdrawal_principals`), burns ckETH, enqueues the request into `pending_withdrawal_requests`, drops the guard (principal removed from `pending_withdrawal_principals`). After all 100 calls, `withdrawal_requests_len() == 100`.
4. Any subsequent `withdraw_eth` or `withdraw_erc20` call from any principal now hits `pending_requests_count(s) >= MAX_PENDING` and `ic_cdk::trap`s with `TooManyPendingRequests`.
5. Wait ~12 minutes for Ethereum confirmation. Repeat from step 3.

A deterministic integration test using PocketIC can reproduce this by submitting 100 withdrawal calls from distinct principals and asserting that the 101st call returns a `CANISTER_ERROR` rejection.

### Citations

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L32-34)
```rust
    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L50-54)
```rust
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
            }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L35-39)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum WithdrawalRequest {
    CkEth(EthWithdrawalRequest),
    CkErc20(Erc20WithdrawalRequest),
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L929-931)
```rust
    pub fn withdrawal_requests_len(&self) -> usize {
        self.pending_withdrawal_requests.len()
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L401-405)
```rust
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L39-41)
```rust
const WITHDRAWAL_REQUESTS_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SIGN_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SEND_BATCH_SIZE: usize = 5;
```
