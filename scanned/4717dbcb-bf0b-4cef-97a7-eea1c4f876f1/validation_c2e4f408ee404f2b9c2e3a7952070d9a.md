### Title
Single-Principal Withdrawal Queue Saturation via Sequential Minimum-Amount Calls - (`rs/ethereum/cketh/minter/src/guard/mod.rs`)

### Summary

The ckETH minter's `retrieve_withdraw_guard` only prevents **concurrent** in-flight calls from the same principal, but places no per-principal cap on the number of requests that can accumulate in `pending_withdrawal_requests`. A single unprivileged principal can make 100 sequential minimum-amount `withdraw_eth` (or `withdraw_erc20`) calls, filling the entire `MAX_PENDING = 100` queue and blocking all other users from withdrawing until the attacker's requests are drained.

### Finding Description

The guard in `rs/ethereum/cketh/minter/src/guard/mod.rs` enforces two limits:

1. `AlreadyProcessing` — rejects a new call from principal P if P already has an **in-flight** (async-awaiting) call.
2. `TooManyPendingRequests` — rejects any new call when `withdrawal_requests_len() >= MAX_PENDING` (100). [1](#0-0) 

The guard is a Rust RAII object dropped when `withdraw_eth` returns: [2](#0-1) 

The `Drop` impl removes the principal from `pending_withdrawal_principals`. However, by the time the guard is dropped, `process_event(AcceptedEthWithdrawalRequest)` has already appended the request to `pending_withdrawal_requests`: [3](#0-2) 

After the function returns, the principal is no longer in `pending_withdrawal_principals`, so the **same principal can immediately call `withdraw_eth` again**. The `AlreadyProcessing` check is bypassed because it only tracks in-flight calls, not queued requests. The attacker repeats this until `withdrawal_requests_len() == 100`, at which point every other caller receives `TooManyPendingRequests`.

The timer-driven processing dequeues only `WITHDRAWAL_REQUESTS_BATCH_SIZE = 5` requests per tick: [4](#0-3) [5](#0-4) 

So 100 attacker requests require 20 timer ticks to drain, during which no legitimate withdrawal can be accepted.

The same guard and `withdraw_erc20` endpoint share this path: [6](#0-5) 

### Impact Explanation

Any non-anonymous principal holding enough ckETH (100 × minimum withdrawal amount) can saturate the 100-slot pending queue. During saturation, every other user's `withdraw_eth` or `withdraw_erc20` call is rejected with `TemporarilyUnavailable("TooManyPendingRequests")`. The attacker's funds are not lost — they are converted to ETH and returned to their Ethereum address — so the only cost is Ethereum gas fees embedded in the withdrawal amounts. The DOS window lasts until all 100 attacker requests are processed (20 timer ticks × tick interval). [7](#0-6) 

### Likelihood Explanation

The attack is reachable by any unprivileged ingress sender with no special role. The attacker needs to hold ckETH equal to 100 × `cketh_minimum_withdrawal_amount` and issue 100 sequential update calls. Because the ckETH is returned as ETH (minus gas), the net cost is only Ethereum transaction fees — a recoverable, bounded expense. The attack can be repeated indefinitely after each drain cycle. [8](#0-7) 

### Recommendation

Add a per-principal cap on the number of requests allowed in `pending_withdrawal_requests`. Before inserting a new request, count how many existing pending requests share the same `from` principal and reject if the count exceeds a small threshold (e.g., 3–5). This mirrors the fix suggested in the external report (limit withdrawal requests per address) and is independent of the in-flight concurrency guard.

Alternatively, raise the minimum withdrawal amount to make queue-filling economically prohibitive, or implement a priority queue ordered by amount so that large legitimate withdrawals are not starved by many small attacker requests.

### Proof of Concept

1. Attacker holds 100 × `cketh_minimum_withdrawal_amount` ckETH.
2. Attacker calls `withdraw_eth(min_amount, attacker_eth_addr)` — Call 1 completes, request enters `pending_withdrawal_requests` (len = 1), guard dropped.
3. Attacker immediately calls `withdraw_eth` again — guard check: `pending_requests_count` = 1 < 100, principal not in `pending_withdrawal_principals` → guard acquired. Call 2 completes, len = 2.
4. Repeat until len = 100 (`MAX_PENDING`).
5. Any legitimate user calling `withdraw_eth` now receives `ic_cdk::trap("Failed retrieving guard … TooManyPendingRequests")`.
6. The minter timer drains 5 requests per tick; during ~20 ticks the queue is occupied exclusively by attacker requests. [7](#0-6) [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L50-68)
```rust
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
            }
            let principals = PR::guarded_principals(s);
            if principals.contains(&principal) {
                return Err(GuardError::AlreadyProcessing);
            }
            if principals.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            principals.insert(principal);
            Ok(Self {
                principal,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L71-75)
```rust
impl<PR: RequestsGuardedByPrincipal> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::guarded_principals(s).remove(&self.principal));
    }
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L330-336)
```rust
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
