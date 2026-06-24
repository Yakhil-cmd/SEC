### Title
Single Principal Can Exhaust the Global ckBTC Withdrawal Queue, Blocking All Other Users - (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The ckBTC minter enforces a single global cap of `MAX_CONCURRENT_PENDING_REQUESTS = 5000` on pending withdrawal requests with no per-principal sub-limit. A single unprivileged user with sufficient ckBTC balance can sequentially submit minimum-amount withdrawal requests until the global queue is full, causing every subsequent `retrieve_btc` or `retrieve_btc_with_approval` call from any other user to fail with `TemporarilyUnavailable("too many pending retrieve_btc requests")` until the attacker's requests are processed on the Bitcoin network (hours to days).

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, both `retrieve_btc` and `retrieve_btc_with_approval` check the global queue depth before accepting a new request:

```rust
if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
{
    return Err(RetrieveBtcError::TemporarilyUnavailable(
        "too many pending retrieve_btc requests".to_string(),
    ));
}
``` [1](#0-0) [2](#0-1) 

The only per-account protection is `retrieve_btc_guard`, which prevents two **concurrent** in-flight calls for the same `Account` (owner + subaccount pair):

```rust
impl<PR: PendingRequests> Guard<PR> {
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            ...
        })
    }
}
``` [3](#0-2) 

Critically, the guard is **dropped when the async function returns** (after the burn is committed to the ledger and the request is added to `pending_retrieve_btc_requests`). This means sequential calls from the same account are not blocked. There is no per-principal limit on how many entries a single account can accumulate in `pending_retrieve_btc_requests`. [4](#0-3) 

The `retrieve_btc_with_approval` path additionally allows a single principal to use **different subaccounts** (`from_subaccount` field), each treated as a distinct `Account`, bypassing even the concurrent guard:

```rust
let caller_account = Account {
    owner: caller,
    subaccount: args.from_subaccount,
};
let _guard = retrieve_btc_guard(caller_account)?;
``` [5](#0-4) 

Once a request is accepted, it is pushed unconditionally into `pending_retrieve_btc_requests` with no per-account cap:

```rust
pub fn accept_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    ...
) {
    ...
    state.pending_retrieve_btc_requests.push(request.clone());
``` [6](#0-5) 

---

### Impact Explanation

Once the global queue reaches 5000 entries (all owned by the attacker), every call to `retrieve_btc` or `retrieve_btc_with_approval` from any other user returns `TemporarilyUnavailable`. The queue drains only as Bitcoin transactions confirm on-chain (minimum ~10 minutes per block, but the minter batches requests and waits for `min_confirmations`). During this window, legitimate ckBTC holders cannot convert their tokens to BTC. This is a direct DoS of the ckBTC withdrawal system affecting all users of the mainnet canister `mqygn-kiaaa-aaaar-qaadq-cai`. [2](#0-1) 

---

### Likelihood Explanation

The attacker must hold `5000 × fee_based_retrieve_btc_min_amount` ckBTC at the time of the attack. The minimum is dynamically computed from current Bitcoin fees but is typically on the order of tens of thousands of satoshis per request. The ckBTC is burned upfront but the attacker eventually receives BTC back (minus miner fees), so the net cost is only the Bitcoin transaction fees for the batched outputs. For a well-funded attacker, this is economically feasible. The attack requires no privileged access — any unprivileged ingress caller can trigger it. [7](#0-6) 

---

### Recommendation

- **Short term:** Add a per-principal (or per-account) cap on the number of entries in `pending_retrieve_btc_requests`. For example, reject a new request if the caller already has `N` (e.g., 10–50) incomplete requests. Track this via `retrieve_btc_account_to_block_indices`.
- **Long term:** Review the global `MAX_CONCURRENT_PENDING_REQUESTS` limit in relation to the number of active users and the Bitcoin block time, and document the queue-full behavior clearly so users understand the `TemporarilyUnavailable` response. [8](#0-7) 

---

### Proof of Concept

1. Eve acquires `5000 × fee_based_retrieve_btc_min_amount` ckBTC (e.g., by depositing BTC to 5000 different subaccount addresses).
2. Eve calls `retrieve_btc_with_approval` 5000 times, each with a distinct `from_subaccount` value (e.g., `[0;32]`, `[1;32]`, …, `[4999;32]`), each for the minimum amount. Each call passes the guard (different `Account` keys), passes the queue-depth check (queue not yet full), burns ckBTC, and enqueues a `RetrieveBtcRequest`.
3. After 5000 successful calls, `count_incomplete_retrieve_btc_requests()` equals `MAX_CONCURRENT_PENDING_REQUESTS = 5000`.
4. Alice calls `retrieve_btc_with_approval` for a legitimate withdrawal. The check at line 274 fires and returns `TemporarilyUnavailable("too many pending retrieve_btc requests")`.
5. Alice (and all other users) are blocked until the Bitcoin network confirms Eve's 5000 transactions and the minter finalizes them — a window of hours to days. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-170)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L174-179)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L259-263)
```rust
    let caller_account = Account {
        owner: caller,
        subaccount: args.from_subaccount,
    };
    let _guard = retrieve_btc_guard(caller_account)?;
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L63-67)
```rust
impl<PR: PendingRequests> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::pending_requests(s).remove(&self.account));
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L17-33)
```rust
pub fn accept_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    runtime: &R,
) {
    record_event(
        EventType::AcceptedRetrieveBtcRequest(request.clone()),
        runtime,
    );
    state.pending_retrieve_btc_requests.push(request.clone());
    if let Some(account) = request.reimbursement_account {
        state
            .retrieve_btc_account_to_block_indices
            .entry(account)
            .and_modify(|entry| entry.push(request.block_index))
            .or_insert(vec![request.block_index]);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L459-463)
```rust
    /// Retrieve_btc requests that are waiting to be served, sorted by received_at.
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,

    /// Maps Account to its retrieve_btc requests burn block indices.
    pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
```

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
