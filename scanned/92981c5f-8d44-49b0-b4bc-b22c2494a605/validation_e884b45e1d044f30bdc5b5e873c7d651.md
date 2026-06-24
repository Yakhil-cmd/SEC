### Title
Unprivileged Caller Can Exhaust the Global `pending_retrieve_btc_requests` Queue, Causing Temporary DOS of ckBTC Withdrawals - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

### Summary

The ckBTC minter enforces a global cap of 5,000 on the total number of incomplete withdrawal requests (`MAX_CONCURRENT_PENDING_REQUESTS`). There is no per-account limit on how many accepted requests a single principal can accumulate in `pending_retrieve_btc_requests`. An unprivileged caller holding sufficient ckBTC can sequentially submit minimum-amount `retrieve_btc` or `retrieve_btc_with_approval` calls until the global cap is reached, after which every legitimate user's withdrawal attempt returns `TemporarilyUnavailable("too many pending retrieve_btc requests")`. The attacker recovers their BTC minus fees once the minter processes the flood of requests, making the attack economically repeatable.

### Finding Description

`retrieve_btc` and `retrieve_btc_with_approval` both check the global queue size before accepting a new request:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs, line 174
if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS) {
    return Err(RetrieveBtcError::TemporarilyUnavailable(
        "too many pending retrieve_btc requests".to_string(),
    ));
}
```

`MAX_CONCURRENT_PENDING_REQUESTS` is a module-level constant set to `5000`. [1](#0-0) 

The `retrieve_btc_guard` (backed by `retrieve_btc_accounts: BTreeSet<Account>`) only prevents a single account from having more than one **concurrent in-flight** async call at a time, with a global ceiling of `MAX_CONCURRENT = 100` simultaneous in-flight calls. [2](#0-1) 

Critically, the guard is dropped as soon as the async function returns. Once a request is accepted and written to `pending_retrieve_btc_requests`, the guard is released and the same account is immediately eligible to submit another request. [3](#0-2) 

There is no per-account cap on the number of entries in `pending_retrieve_btc_requests`. The `retrieve_btc_account_to_block_indices` map records which block indices belong to each account for status queries, but it is never consulted to limit how many pending requests an account may hold. [4](#0-3) 

`count_incomplete_retrieve_btc_requests()` sums pending + in-flight + submitted-but-unconfirmed requests across all accounts with no per-account breakdown. [5](#0-4) 

### Impact Explanation

Once the global queue reaches 5,000 entries, both `retrieve_btc` and `retrieve_btc_with_approval` return `TemporarilyUnavailable` to every caller, including legitimate users who hold ckBTC and want to convert it to BTC. The DOS persists until the minter's timer loop processes enough of the attacker's requests to drain the queue below the cap. Because the attacker's requests are valid (real ckBTC is burned and real BTC is eventually sent back), the minter cannot distinguish them from legitimate requests and must process them in FIFO order. The attacker recovers their BTC minus minter and Bitcoin transaction fees, making the attack economically repeatable.

### Likelihood Explanation

The minimum withdrawal amount is `fee_based_retrieve_btc_min_amount`, currently ~100,000 satoshis (0.001 BTC) on mainnet. Filling the queue requires 5,000 × 0.001 BTC = **5 BTC** of working capital. The attacker recovers the principal; the only sunk cost is the minter fee (~305 satoshis per request) plus the Bitcoin transaction fee, totalling on the order of a few hundred satoshis per slot. At current prices this is a few hundred USD to sustain the attack indefinitely. The attack requires no privileged access, no leaked keys, and no governance majority — only a funded ckBTC account and the ability to call a public canister endpoint.

### Recommendation

Introduce a per-account cap on the number of accepted (pending + in-flight + submitted) withdrawal requests. For example, reject a new request if the calling account already has `N` (e.g., 10) incomplete requests recorded in `retrieve_btc_account_to_block_indices`. This preserves the global cap as a secondary safety valve while preventing any single account from monopolising the queue.

### Proof of Concept

1. Attacker acquires 5 BTC worth of ckBTC across one or more accounts (using subaccounts for `retrieve_btc_with_approval` to parallelize).
2. Attacker calls `retrieve_btc_with_approval` (or `retrieve_btc`) with `amount = fee_based_retrieve_btc_min_amount` and a fresh Bitcoin address, 5,000 times sequentially (or up to 100 in parallel using distinct subaccounts).
3. Each call passes the guard check (different subaccount or sequential), passes the queue-size check (count < 5,000), passes the balance check, passes the BTC address check, burns ckBTC, and appends a `RetrieveBtcRequest` to `pending_retrieve_btc_requests`. [6](#0-5) 
4. After 5,000 accepted requests, `count_incomplete_retrieve_btc_requests() == 5000`. [5](#0-4) 
5. Any subsequent call by a legitimate user hits the cap check at line 174 and receives `TemporarilyUnavailable("too many pending retrieve_btc requests")`. [7](#0-6) 
6. The minter's timer processes the attacker's requests in batches (`build_batch` / `submit_pending_requests`), eventually draining the queue. The attacker receives BTC at their addresses and can repeat the attack. [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-60)
```rust
const MAX_CONCURRENT: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
}

pub trait PendingRequests {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account>;
}

pub struct PendingBalanceUpdates;

impl PendingRequests for PendingBalanceUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.update_balance_accounts
    }
}
pub struct RetrieveBtcUpdates;

impl PendingRequests for RetrieveBtcUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.retrieve_btc_accounts
    }
}

/// Guards a block from executing twice when called by the same user and from being
/// executed [MAX_CONCURRENT] or more times in parallel.
#[must_use]
pub struct Guard<PR: PendingRequests> {
    account: Account,
    _marker: PhantomData<PR>,
}

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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L63-66)
```rust
impl<PR: PendingRequests> Drop for Guard<PR> {
    fn drop(&mut self) {
        mutate_state(|s| PR::pending_requests(s).remove(&self.account));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L459-463)
```rust
    /// Retrieve_btc requests that are waiting to be served, sorted by received_at.
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,

    /// Maps Account to its retrieve_btc requests burn block indices.
    pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L942-958)
```rust
    /// Forms a batch of retrieve_btc requests that the minter can fulfill.
    pub fn build_batch(&mut self, max_size: usize) -> BTreeSet<RetrieveBtcRequest> {
        let available_utxos_value = self.available_utxos.iter().map(|u| u.value).sum::<u64>();
        let mut batch = BTreeSet::new();
        let mut tx_amount = 0;
        for req in std::mem::take(&mut self.pending_retrieve_btc_requests) {
            if available_utxos_value < req.amount + tx_amount || batch.len() >= max_size {
                // Put this request back to the queue until we have enough liquid UTXOs.
                self.pending_retrieve_btc_requests.push(req);
            } else {
                tx_amount += req.amount;
                batch.insert(req);
            }
        }

        batch
    }
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
