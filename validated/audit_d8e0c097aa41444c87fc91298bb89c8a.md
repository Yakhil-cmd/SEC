### Title
ckBTC Minter `count_incomplete_retrieve_btc_requests()` Ignores In-Flight Concurrent Requests, Allowing `MAX_CONCURRENT_PENDING_REQUESTS` Safety Limit Bypass - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

---

### Summary

Both `retrieve_btc` and `retrieve_btc_with_approval` in the ckBTC minter perform a safety check against `MAX_CONCURRENT_PENDING_REQUESTS` (5000) by calling `count_incomplete_retrieve_btc_requests()`. This function only reads committed state and ignores the up-to-100 concurrent callers whose requests are currently in-flight (held in the `retrieve_btc_accounts` guard set). Because the check occurs before multiple async inter-canister calls yield execution, up to `MAX_CONCURRENT` (100) concurrent callers can all observe the same stale count, all pass the check, and all ultimately commit their requests — exceeding the intended limit.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, both `retrieve_btc` (line 174) and `retrieve_btc_with_approval` (line 274) execute the following pattern:

```
1. acquire guard → adds account to `retrieve_btc_accounts` (atomic)
2. check: count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS
3. await balance_of(...)          ← yields execution
4. await check_address(...)       ← yields execution
5. await burn_ckbtcs(...)         ← yields execution
6. mutate_state → accept_retrieve_btc_request (adds to pending_retrieve_btc_requests)
```

The safety check at step 2 calls `count_incomplete_retrieve_btc_requests()`:

```rust
pub fn count_incomplete_retrieve_btc_requests(&self) -> usize {
    self.pending_retrieve_btc_requests.len()
        + self.requests_in_flight.len()
        + self.submitted_transactions.iter()
            .map(|tx| tx.requests.count_retrieve_btc_requests())
            .sum::<usize>()
}
``` [1](#0-0) 

This function counts only committed state. It does **not** include accounts currently held in `retrieve_btc_accounts` — the guard set populated by `Guard<RetrieveBtcUpdates>`:

```rust
impl PendingRequests for RetrieveBtcUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.retrieve_btc_accounts
    }
}
``` [2](#0-1) 

The guard only prevents the **same account** from having concurrent requests and caps total concurrent callers at `MAX_CONCURRENT` (100): [3](#0-2) 

Because the check at step 2 is performed before the three async yields (steps 3–5), up to 100 different accounts can all read the same committed count simultaneously, all pass the check, and all proceed to commit their requests at step 6.

The check site in `retrieve_btc`: [4](#0-3) 

The check site in `retrieve_btc_with_approval`: [5](#0-4) 

---

### Impact Explanation

The `MAX_CONCURRENT_PENDING_REQUESTS` (5000) limit is a safety rail that prevents the minter's pending queue from growing unboundedly. When the check ignores the up-to-100 in-flight concurrent requests, the actual committed queue can reach `5000 + 100 = 5100` entries before any caller is rejected. An attacker controlling 100 distinct accounts can deliberately drive the queue 2% above the intended ceiling. While this does not break conservation of ckBTC or allow unauthorized minting/burning, it degrades the minter's ability to enforce its own resource limits and can cause processing delays for legitimate users.

---

### Likelihood Explanation

The attack requires up to 100 concurrent callers with distinct accounts, each submitting a `retrieve_btc` or `retrieve_btc_with_approval` call at the same time. This is straightforwardly achievable by any unprivileged actor who controls 100 principals (or uses 100 subaccounts). No privileged access, key material, or subnet-majority corruption is required. The attacker only needs to time the calls so they all pass the count check before any of them commits.

---

### Recommendation

Move the `count_incomplete_retrieve_btc_requests()` check to also include the number of accounts currently held in the guard set (`retrieve_btc_accounts.len()`), or perform the check atomically inside `mutate_state` at the point of guard acquisition so that the guard count and the committed count are evaluated together. For example, `count_incomplete_retrieve_btc_requests()` could be extended to add `self.retrieve_btc_accounts.len()`, or the guard's `new()` function could incorporate the pending-request count check alongside the concurrent-caller check.

---

### Proof of Concept

1. Observe that `MAX_CONCURRENT_PENDING_REQUESTS = 5000` and `MAX_CONCURRENT = 100`.
2. Arrange for the minter to have exactly 4999 incomplete requests (pending + in-flight + submitted).
3. Submit 100 `retrieve_btc` calls from 100 distinct accounts simultaneously.
4. Each call acquires its guard (atomically, guard set grows to 100 entries).
5. Each call reads `count_incomplete_retrieve_btc_requests()` = 4999 (guard set not counted) → all 100 pass the check.
6. All 100 proceed through `balance_of`, `check_address`, and `burn_ckbtcs` async calls.
7. All 100 commit via `accept_retrieve_btc_request`.
8. Final committed count = 4999 + 100 = 5099, exceeding `MAX_CONCURRENT_PENDING_REQUESTS` by 99. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L960-970)
```rust
    /// Returns the total number of all retrieve_btc requests that we haven't
    /// finalized yet.
    pub fn count_incomplete_retrieve_btc_requests(&self) -> usize {
        self.pending_retrieve_btc_requests.len()
            + self.requests_in_flight.len()
            + self
                .submitted_transactions
                .iter()
                .map(|tx| tx.requests.count_retrieve_btc_requests())
                .sum::<usize>()
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L25-31)
```rust
pub struct RetrieveBtcUpdates;

impl PendingRequests for RetrieveBtcUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.retrieve_btc_accounts
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L41-60)
```rust
impl<PR: PendingRequests> Guard<PR> {
    /// Attempts to create a new guard for the current block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
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
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L162-179)
```rust
    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }

    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L232-232)
```rust
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
