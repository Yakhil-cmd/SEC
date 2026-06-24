### Title
TOCTOU Race on `count_incomplete_retrieve_btc_requests` Allows Exceeding Pending-Request Cap - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

### Summary
In both `retrieve_btc` and `retrieve_btc_with_approval`, the guard against exceeding `MAX_CONCURRENT_PENDING_REQUESTS` is evaluated **before** three inter-canister await points. Because the Internet Computer suspends execution at every `await`, other ingress messages are processed in the gaps. Multiple concurrent callers using distinct accounts can each observe a count below the cap, proceed through all three awaits, and then each commit a new pending request — collectively pushing the queue well past the intended limit.

### Finding Description

`retrieve_btc` executes the following sequence:

1. **Acquire per-account guard** (prevents the *same* account from having two concurrent calls, but does nothing for distinct accounts).
2. **Check the global cap** — `count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS` — and return early if exceeded.
3. **`balance_of(caller).await`** — inter-canister call to the ckBTC ledger; execution is suspended here.
4. **`check_address(...).await`** — inter-canister call to the BTC checker canister; execution is suspended again.
5. **`burn_ckbtcs(caller, args.amount, ...).await`** — inter-canister call to the ckBTC ledger; execution is suspended a third time.
6. **`mutate_state(|s| accept_retrieve_btc_request(s, request, ...))`** — the new request is appended to `pending_retrieve_btc_requests`. [1](#0-0) 

The cap check at step 2 reads a snapshot of the live state. During each of the three await points (steps 3–5), the IC scheduler can deliver other ingress messages, including additional `retrieve_btc` calls from different accounts. Each of those concurrent calls will also read the same (still-unchanged) count, pass the cap check, and eventually reach step 6. Because `accept_retrieve_btc_request` is called independently by every concurrent invocation, the final queue length can exceed `MAX_CONCURRENT_PENDING_REQUESTS` by as many concurrent callers as were in-flight simultaneously. [2](#0-1) [3](#0-2) 

The identical pattern exists in `retrieve_btc_with_approval`: [4](#0-3) [5](#0-4) 

The per-account guard (`retrieve_btc_guard`) only prevents the *same* `Account` from having two concurrent requests; it provides no protection against distinct accounts racing through the same window. [6](#0-5) 

The `count_incomplete_retrieve_btc_requests` function aggregates pending, in-flight, and submitted transactions: [7](#0-6) 

### Impact Explanation

An attacker controlling many distinct ckBTC accounts (or coordinating with other users) can flood the `pending_retrieve_btc_requests` queue beyond its intended cap. Consequences include:

- **Resource exhaustion**: unbounded growth of the pending queue consumes heap memory inside the minter canister.
- **Batch-processing disruption**: `build_batch` iterates over the entire pending queue on every timer tick; an oversized queue increases per-tick cost and can cause the timer to exceed its instruction limit, stalling all BTC withdrawals for all users.
- **Invariant violation**: downstream logic that assumes the queue is bounded (e.g., fee estimation, UTXO selection) may behave incorrectly. [8](#0-7) 

### Likelihood Explanation

The attack requires only unprivileged ingress calls to the publicly reachable `retrieve_btc` / `retrieve_btc_with_approval` endpoints. Each call must hold a small ckBTC balance (enough to pass the `balance_of` check and the subsequent burn). An attacker can pre-fund many subaccounts cheaply, submit all calls in the same IC round, and rely on the three inter-canister await points to create the necessary concurrency window. No privileged access, governance majority, or threshold-key compromise is needed.

### Recommendation

Move the `count_incomplete_retrieve_btc_requests` check to **after** all inter-canister awaits, immediately before `accept_retrieve_btc_request`, so it reflects the live queue length at commit time:

```rust
// After burn_ckbtcs returns successfully:
if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS) {
    // burn already happened — must reimburse the caller here
    return Err(...);
}
mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, ...));
```

Because the burn has already been committed at that point, a rejection requires a compensating re-mint. Alternatively, reserve a slot in the pending queue **atomically before the first await** (analogous to the `NotificationStatus::Processing` sentinel used in the CMC's `notify_top_up`), and release it on any error path. [9](#0-8) 

### Proof of Concept

1. Pre-fund N distinct ckBTC accounts (N > `MAX_CONCURRENT_PENDING_REQUESTS`), each with `retrieve_btc_min_amount` satoshis.
2. Submit N concurrent `retrieve_btc` ingress messages (one per account) in the same IC round.
3. Each call passes the cap check (the queue has not yet grown) and enters the `balance_of` await.
4. The IC scheduler interleaves the callbacks: all N calls complete their three awaits and each calls `accept_retrieve_btc_request`.
5. Observe `count_incomplete_retrieve_btc_requests()` returning a value greater than `MAX_CONCURRENT_PENDING_REQUESTS`, confirming the cap was bypassed.
6. Repeat to grow the queue arbitrarily, eventually stalling the minter's timer-driven batch processing.

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L314-333)
```rust
    let block_index = burn_ckbtcs_icrc2(
        caller_account,
        args.amount,
        crate::memo::encode(&burn_memo_icrc2).into(),
    )
    .await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: args.from_subaccount,
        }),
    };

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, runtime));
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L943-958)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L962-970)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1172-1207)
```rust
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }

        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),

                // If the user makes a duplicate request, we respond as though
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as create canister request".into(),
                    )))
                }
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as mint request".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as automatic refund".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
    });
```
