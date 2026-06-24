### Title
Lack of Per-User Rate Limiting on ckETH/ckERC20 Withdrawal Queue Enables Sustained DoS on All Withdrawals - (File: `rs/ethereum/cketh/minter/src/guard/mod.rs`)

---

### Summary

The ckETH minter enforces a single global cap of `MAX_PENDING = 100` on pending withdrawal requests shared across all users and all token types (ckETH and ckERC20), with no per-principal sub-limit. An unprivileged attacker controlling 100 distinct IC principals can fill this queue entirely, causing every subsequent `withdraw_eth` or `withdraw_erc20` call to `ic_cdk::trap` (hard reject, not a graceful error) until the attacker's Ethereum transactions confirm. Because the attacker recovers their ETH after each cycle, the attack can be repeated indefinitely at the cost of Ethereum gas fees alone.

---

### Finding Description

**Root cause — `rs/ethereum/cketh/minter/src/guard/mod.rs`**

```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;

impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()   // ← global count, no per-user split
    }
}

fn new(principal: Principal) -> Result<Self, GuardError> {
    mutate_state(|s| {
        if PR::pending_requests_count(s) >= MAX_PENDING {   // ← single global gate
            return Err(GuardError::TooManyPendingRequests);
        }
        ...
    })
}
```

`withdrawal_requests_len()` counts every entry in `pending_withdrawal_requests` regardless of who submitted it. [1](#0-0) [2](#0-1) [3](#0-2) 

**Trap on guard failure — `rs/ethereum/cketh/minter/src/main.rs`**

Both public withdrawal endpoints call `ic_cdk::trap` (not `return Err(...)`) when the guard is denied:

```rust
// withdraw_eth (line 274)
let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
    ic_cdk::trap(format!("Failed retrieving guard for principal {caller}: {e:?}"))
});

// withdraw_erc20 (line 401)
let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
    ic_cdk::trap(format!("Failed retrieving guard for principal {caller}: {e:?}"))
});
```

A trap causes the IC to reject the call with `CANISTER_ERROR`. The caller's tokens are **not** burned (the burn happens after the guard), so funds are safe, but the withdrawal is completely blocked. [4](#0-3) [5](#0-4) 

**Queue drains slowly — `rs/ethereum/cketh/minter/src/withdraw.rs`**

The minter processes only `WITHDRAWAL_REQUESTS_BATCH_SIZE = 5` requests per timer tick. Once a request is picked up it moves from `pending_withdrawal_requests` to `created_tx`, decrementing `withdrawal_requests_len()`. However, the Ethereum transaction must then be signed via threshold ECDSA and confirmed on-chain (~12 minutes). During this window the queue slot is freed for new legitimate requests, but the attacker can immediately re-submit to refill it. [6](#0-5) 

**Shared counter covers both ckETH and ckERC20**

`pending_withdrawal_requests` is a single `VecDeque<WithdrawalRequest>` holding both `CkEth` and `CkErc20` variants. An attacker can use cheap ckERC20 tokens (if available) to fill the 100-slot queue and block ckETH withdrawals, or vice versa. [7](#0-6) [8](#0-7) 

---

### Impact Explanation

- **Complete DoS on all ckETH and ckERC20 withdrawals** for the duration of each attack cycle.
- Every `withdraw_eth` / `withdraw_erc20` call from any legitimate user traps with `TooManyPendingRequests` while the queue is saturated.
- The attacker recovers their ETH after Ethereum confirmation, so the only sustained cost is Ethereum gas fees (~$500/cycle at current prices), making indefinite DoS economically feasible.
- Users whose withdrawals are blocked cannot access their funds on the IC side (ckETH is already burned in the normal flow; here the burn is prevented, but users are still unable to exit).

---

### Likelihood Explanation

- **No privileged access required.** Any unprivileged IC user can generate 100 distinct principals trivially (100 key pairs).
- **Capital requirement is modest.** 100 × minimum withdrawal amount (≈ 0.03 ETH each) = ~3 ETH (~$10,000 at current prices), fully recovered after each cycle.
- **Repeatable.** As the minter drains the queue (5 requests per timer tick), the attacker re-submits to keep it saturated.
- **No on-chain complexity.** The attack requires only standard IC ingress calls to `withdraw_eth`.

---

### Recommendation

1. **Add a per-principal pending-request cap** inside `Guard::new`: reject if the calling principal already has `N` requests in `pending_withdrawal_requests` (e.g., `N = 3`).
2. **Return a graceful error instead of trapping** when `TooManyPendingRequests` is hit, so callers receive a retryable `TemporarilyUnavailable` response rather than a hard `CANISTER_ERROR`.
3. **Raise `MAX_PENDING`** to a value that makes queue saturation economically prohibitive, or tie it to a per-principal sub-quota.
4. **Separate the ckETH and ckERC20 pending counters** so that flooding one token type cannot block the other.

---

### Proof of Concept

1. Generate 100 IC key pairs → 100 distinct principals `P_1 … P_100`.
2. Fund each principal with ≥ `cketh_minimum_withdrawal_amount` ckETH (≈ 0.03 ETH each) via the ckETH ledger.
3. Concurrently call `withdraw_eth` from each principal with the minimum amount to a valid Ethereum address.
   - IC processes these sequentially; each call passes the `MAX_PENDING` check (queue starts at 0) and the `MAX_CONCURRENT` check (≤ 100 in-flight guards), burns ckETH, and enqueues the request.
   - After all 100 calls complete, `withdrawal_requests_len() == 100`.
4. Any subsequent `withdraw_eth` or `withdraw_erc20` call from any principal now hits `pending_requests_count(s) >= MAX_PENDING` and `ic_cdk::trap`s.
5. Wait ~12 minutes for the minter to process the batch and Ethereum to confirm. Repeat from step 3.

**Expected outcome:** Legitimate users receive `CANISTER_ERROR` rejections on all withdrawal attempts for the duration of each cycle. The attacker's ETH is returned after confirmation, sustaining the attack indefinitely at gas-fee cost only.

### Citations

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L453-466)
```rust
    pub fn record_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        let burn_index = request.cketh_ledger_burn_index();
        if self
            .pending_withdrawal_requests
            .iter()
            .any(|r| r.cketh_ledger_burn_index() == burn_index)
            || self.created_tx.contains_alt(&burn_index)
            || self.sent_tx.contains_alt(&burn_index)
            || self.finalized_tx.contains_alt(&burn_index)
        {
            panic!("BUG: duplicate ckETH ledger burn index {burn_index}");
        }
        self.pending_withdrawal_requests.push_back(request);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L929-931)
```rust
    pub fn withdrawal_requests_len(&self) -> usize {
        self.pending_withdrawal_requests.len()
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L273-278)
```rust
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L400-405)
```rust
    let caller = validate_caller_not_anonymous();
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
