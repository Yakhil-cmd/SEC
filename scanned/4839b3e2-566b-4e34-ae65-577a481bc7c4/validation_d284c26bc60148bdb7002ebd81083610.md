### Title
Attacker Can DoS ckETH/ckERC20 Withdrawals by Flooding the Global Pending Withdrawal Queue — (File: `rs/ethereum/cketh/minter/src/guard/mod.rs`)

---

### Summary

The ckETH minter enforces a global cap of `MAX_PENDING = 100` on pending withdrawal requests. An unprivileged attacker controlling 100 IC principals, each holding the minimum withdrawal amount of ckETH (currently 0.005 ETH ≈ $10), can fill the shared `pending_withdrawal_requests` queue and cause every subsequent `withdraw_eth` and `withdraw_erc20` call from any legitimate user to trap, effectively DoS-ing all ckETH and ckERC20 withdrawals.

---

### Finding Description

**Root cause — `rs/ethereum/cketh/minter/src/guard/mod.rs`**

`retrieve_withdraw_guard` checks a global pending-request count before admitting a new withdrawal:

```rust
pub const MAX_PENDING: usize = 100;

fn new(principal: Principal) -> Result<Self, GuardError> {
    mutate_state(|s| {
        if PR::pending_requests_count(s) >= MAX_PENDING {   // ← global cap
            return Err(GuardError::TooManyPendingRequests);
        }
        ...
    })
}
``` [1](#0-0) [2](#0-1) 

`pending_requests_count` is wired to `withdrawal_requests_len()`, which counts entries in the `pending_withdrawal_requests` VecDeque — the FIFO queue of requests not yet assigned an Ethereum nonce:

```rust
impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
}
``` [3](#0-2) [4](#0-3) 

**Guard lifecycle in `withdraw_eth` — `rs/ethereum/cketh/minter/src/main.rs`**

The guard is acquired at the top of `withdraw_eth`, held across the async `burn_from` call, and dropped when the function returns. After a successful burn, `record_withdrawal_request` pushes the request into `pending_withdrawal_requests`. Once the function returns, the guard drops and the principal is removed from `pending_withdrawal_principals` — but the request **remains** in the queue, permanently consuming one of the 100 slots until the minter's timer processes it. [5](#0-4) [6](#0-5) 

Both `withdraw_eth` and `withdraw_erc20` share the same guard and the same `MAX_PENDING = 100` limit, so flooding via either endpoint blocks both. [7](#0-6) 

**Error handling traps the caller**

When the guard returns `TooManyPendingRequests`, both endpoints call `ic_cdk::trap`, which rejects the call with a system error rather than returning a graceful error variant:

```rust
let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
    ic_cdk::trap(format!(
        "Failed retrieving guard for principal {caller}: {e:?}"
    ))
});
``` [8](#0-7) 

**Minter processes only 5 requests per batch**

The timer-driven processing loop dequeues at most `WITHDRAWAL_REQUESTS_BATCH_SIZE = 5` requests per cycle: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

An attacker who fills all 100 slots causes every `withdraw_eth` and `withdraw_erc20` call from any legitimate user to trap. Users holding ckETH or ckERC20 tokens cannot convert them back to ETH/ERC20 for as long as the attacker maintains the queue at capacity. The attacker's funds are not destroyed — they burn ckETH and receive ETH on Ethereum — so the attack can be sustained indefinitely at the cost of Ethereum gas fees only.

---

### Likelihood Explanation

- **Capital required**: 100 principals × 0.005 ETH minimum = 0.5 ETH ≈ $1,000 (recently reduced from 0.03 ETH per the May 2026 upgrade).
- **Ongoing cost**: Only Ethereum gas fees (currently cents per transaction) to cycle ETH → ckETH → ETH.
- **Maintenance**: The minter drains 5 slots per processing cycle; the attacker needs to submit 5 new requests per cycle to maintain the DoS.
- **No privileged access required**: Any unprivileged IC principal can call `withdraw_eth`.

Likelihood is **medium** — the capital barrier is low and the attack is sustainable.

---

### Recommendation

1. **Raise `MAX_PENDING`** to a value that reflects realistic legitimate demand (e.g., 1,000–10,000), consistent with the ckBTC minter's `MAX_CONCURRENT_PENDING_REQUESTS = 5000`.
2. **Add per-principal pending-request limits** so a single principal (or a small set) cannot monopolize the queue.
3. **Return a graceful error** instead of `ic_cdk::trap` for `TooManyPendingRequests`, so callers can distinguish a transient capacity limit from a protocol error and retry.
4. **Consider a minimum fee or deposit** for queuing a withdrawal request to raise the economic cost of flooding.

---

### Proof of Concept

1. Attacker creates 100 IC principals, each funded with ≥ 0.005 ETH worth of ckETH.
2. Each principal calls `icrc2_approve` on the ckETH ledger, approving the minter for the minimum amount.
3. Each principal calls `withdraw_eth` with `amount = minimum_withdrawal_amount` and any valid Ethereum recipient address.
4. All 100 calls succeed; each burns ckETH and enqueues a `WithdrawalRequest`. After all 100 complete, `pending_withdrawal_requests.len() == 100 == MAX_PENDING`.
5. Any legitimate user now calling `withdraw_eth` or `withdraw_erc20` receives a trap: `"Failed retrieving guard for principal X: TooManyPendingRequests"`.
6. The minter processes 5 requests per cycle. The attacker monitors the queue (via `get_minter_info` or event logs) and submits 5 new requests each cycle, maintaining the DoS.
7. The attacker receives ETH on Ethereum for each processed request, recovering their capital minus gas fees.

### Citations

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L27-35)
```rust
impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn guarded_principals(state: &mut State) -> &mut BTreeSet<Principal> {
        &mut state.pending_withdrawal_principals
    }

    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-336)
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
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L39-39)
```rust
const WITHDRAWAL_REQUESTS_BATCH_SIZE: usize = 5;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-253)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
```
