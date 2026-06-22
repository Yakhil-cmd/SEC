### Title
TOCTOU Race on `MAX_CONCURRENT_PENDING_REQUESTS` Check Allows Bounded Queue Overflow — (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

A time-of-check/time-of-use (TOCTOU) race condition exists in both `retrieve_btc` and `retrieve_btc_with_approval`. The `count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS` guard is a plain synchronous read, but it is separated from the actual `mutate_state` enqueue by three inter-canister `await` points. On the IC, execution yields at every `await`, allowing other messages to interleave. Up to `MAX_CONCURRENT` (100) different callers can simultaneously pass the count check before any of them commits the enqueue, pushing the queue beyond 5000.

---

### Finding Description

**Execution flow in `retrieve_btc`:**

| Step | Line | Type |
|------|------|------|
| `init_ecdsa_public_key().await` | 155 | **await** |
| `retrieve_btc_guard(account)` | 162 | sync — atomically inserts account into `retrieve_btc_accounts` |
| `count_incomplete_retrieve_btc_requests() >= 5000` | 174 | sync read — **the check** |
| `balance_of(caller).await` | 181 | **await** — yields execution |
| `check_address(...).await` | 187 | **await** — yields execution |
| `burn_ckbtcs(...).await` | 210 | **await** — yields execution |
| `mutate_state(accept_retrieve_btc_request)` | 232 | sync — **the enqueue** |

The check at line 174 and the enqueue at line 232 are separated by three `await` points. [1](#0-0) [2](#0-1) 

The `retrieve_btc_guard` prevents the **same account** from having two concurrent in-flight calls, and caps total concurrent in-flight calls at `MAX_CONCURRENT = 100`. [3](#0-2) 

It does **not** re-check `count_incomplete_retrieve_btc_requests` atomically with the enqueue. The guard and the pending-queue count are two independent state structures (`retrieve_btc_accounts` BTreeSet vs. `pending_retrieve_btc_requests` Vec). [4](#0-3) [5](#0-4) 

**Race scenario (count = 4999, N ≤ 100 distinct callers):**
1. All N callers acquire their per-account guard (guard allows up to 100 concurrent).
2. All N callers read `count = 4999 < 5000` — all pass the check.
3. All N callers yield at `balance_of().await`, `check_address().await`, `burn_ckbtcs().await`.
4. All N burns succeed on the ledger.
5. All N callers reach `mutate_state(accept_retrieve_btc_request)` and enqueue.
6. Final count = 4999 + N, up to **5099**.

The same race exists identically in `retrieve_btc_with_approval` at line 274. [6](#0-5) 

---

### Impact Explanation

The `count_incomplete_retrieve_btc_requests` invariant — intended to be ≤ 5000 — can be violated by up to `MAX_CONCURRENT` (100) extra entries, reaching at most **5099**. The claimed unbounded memory exhaustion is not achievable because the guard hard-caps concurrent in-flight calls at 100. The actual impact is:

- **Invariant violation**: `count_incomplete_retrieve_btc_requests` exceeds `MAX_CONCURRENT_PENDING_REQUESTS`.
- **Bounded queue bloat**: at most ~100 extra requests beyond the limit.
- **No fund loss**: all burns are committed on the ledger before enqueue; the minter will eventually process all requests.
- **No memory exhaustion**: 100 extra `RetrieveBtcRequest` structs are negligible.

Severity is **low-to-medium**: the invariant is broken, but the operational impact is minor.

---

### Likelihood Explanation

Requires N ≤ 100 distinct principals each holding sufficient ckBTC, coordinating calls when the queue is near 4999. This is realistic for a motivated attacker with multiple wallets. No privileged access is needed.

---

### Recommendation

Move the count check inside the same `mutate_state` closure that performs the enqueue, or re-check the count immediately before enqueue (after all awaits complete) and abort if the limit is exceeded. Example pattern:

```rust
// After burn succeeds, before enqueue:
mutate_state(|s| {
    if s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS {
        // handle: the burn already happened, so reimburse or record for retry
        return Err(...);
    }
    state::audit::accept_retrieve_btc_request(s, request, runtime);
    Ok(())
})?;
```

Because the burn is already committed at this point, the error path must reimburse the caller (or the existing reimbursement flow handles it).

---

### Proof of Concept

```
Precondition: count_incomplete_retrieve_btc_requests() == 4999
Actors: 100 distinct principals P1..P100, each with sufficient ckBTC in their withdrawal subaccount

1. P1..P100 each call retrieve_btc concurrently.
2. Each acquires retrieve_btc_guard (guard.rs MAX_CONCURRENT=100 allows all 100).
3. Each reads count=4999 < 5000 at line 174 — all pass.
4. Each yields at balance_of().await (line 181).
5. Each yields at check_address().await (line 187).
6. Each yields at burn_ckbtcs().await (line 210) — all 100 burns committed on ledger.
7. Each calls mutate_state(accept_retrieve_btc_request) at line 232.
8. Final: count_incomplete_retrieve_btc_requests() == 5099 > 5000.

Assert: count > MAX_CONCURRENT_PENDING_REQUESTS  ✓  (invariant broken)
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L174-179)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-232)
```rust
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: None,
        }),
    };

    log!(
        Priority::Debug,
        "accepted a retrieve btc request for {} BTC to address {} (block_index = {})",
        crate::tx::DisplayAmount(request.amount),
        args.address,
        request.block_index
    );

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L274-279)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L45-59)
```rust
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self {
                account,
                _marker: PhantomData,
            })
        })
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L446-448)
```rust

    /// Per-account lock for retrieve_btc
    pub retrieve_btc_accounts: BTreeSet<Account>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L460-460)
```rust
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,
```
