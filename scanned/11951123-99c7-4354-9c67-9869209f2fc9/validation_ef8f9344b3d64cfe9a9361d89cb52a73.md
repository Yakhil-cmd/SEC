### Title
Unprivileged Caller Can Drain ckBTC Minter Cycles via `update_balance` with Arbitrary Owner Principals - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

### Summary
The ckBTC minter's `update_balance` endpoint allows any non-anonymous caller to specify an arbitrary `owner` principal. Each invocation unconditionally triggers one or two Bitcoin API (`get_utxos`) calls paid from the minter's own cycle balance. Because the per-account concurrency guard is keyed on the supplied `owner` rather than the actual caller, an attacker can saturate the global concurrency limit with 100 distinct owner principals and repeat the pattern indefinitely, draining the minter's cycles without depositing any BTC.

### Finding Description
`update_balance` in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` accepts `UpdateBalanceArgs { owner: Option<Principal>, subaccount: Option<Subaccount> }`. When `owner` is `Some(p)`, the minter derives a Bitcoin address for the account `{owner: p, subaccount}` and calls `get_utxos` against the Bitcoin canister — a call that is charged to the minter's cycle balance. [1](#0-0) 

When no processable UTXOs exist (the common case for a freshly-generated principal with no BTC), the function makes a **second** `get_utxos` call with `min_confirmations = 0` to populate the `pending_utxos` field of the error response. [2](#0-1) 

The only concurrency protection is `balance_update_guard(caller_account)`, which is keyed on the resolved `{owner, subaccount}` pair — not on the actual ingress sender — and a global ceiling of `MAX_CONCURRENT = 100`. [3](#0-2) 

Because the guard is per-account and the attacker controls the `owner` field, 100 distinct owner principals bypass the per-account deduplication entirely. After each batch of 100 concurrent calls completes, the guard set is empty and the attacker can immediately submit the next batch. The `get_utxos_cache` does not help: it is keyed by `(address, filter)`, so each novel principal maps to a unique address and always misses the cache. [4](#0-3) 

The `check_anonymous_caller()` guard in `main.rs` only blocks the anonymous principal; any real IC identity suffices. [5](#0-4) 

### Impact Explanation
Each batch of 100 concurrent calls forces 200 Bitcoin-canister `get_utxos` round-trips billed to the minter. Bitcoin API calls on IC are priced in cycles proportional to response size; the minter bears this cost with no corresponding charge to the caller beyond the standard ingress fee, which is orders of magnitude smaller. Sustained batching progressively depletes the minter's cycle balance. When the balance falls below the freeze threshold the minter stops executing; if it reaches zero the subnet uninstalls it, permanently halting all ckBTC deposit and withdrawal operations for every user.

### Likelihood Explanation
The attack requires only a valid (non-anonymous) IC principal and the ability to submit ingress messages — capabilities available to any IC user. No BTC deposit, no privileged key, and no governance access are needed. The attacker can automate batch submission trivially. The `MAX_CONCURRENT = 100` cap limits parallelism but not throughput over time.

### Recommendation
1. **Restrict the `owner` field**: require `args.owner.unwrap_or(caller) == caller`, so a caller can only trigger balance checks for their own account. This eliminates the ability to enumerate arbitrary addresses.
2. **Per-caller rate limiting**: if cross-account queries must remain supported, track call counts per ingress sender and reject requests that exceed a per-second or per-minute quota.
3. **Charge the caller**: attach a cycle payment requirement to `update_balance` proportional to the expected Bitcoin API cost, refunding any surplus, so the minter is not the net payer for user-initiated queries.

### Proof of Concept
```
# Attacker generates 100 distinct principals p_1 … p_100 (e.g. fresh key pairs)
# and submits 100 concurrent ingress calls:

for i in 1..=100:
    dfx canister call ckbtc_minter update_balance \
        "(record { owner = opt principal \"<p_i>\"; subaccount = null })" &

wait   # all 100 complete; each triggered 2 get_utxos calls from minter's cycles

# Repeat indefinitely — no BTC deposit required, no privileged access needed.
```

Each iteration forces ≥ 200 Bitcoin-canister calls billed to the minter. Repeating at the rate the subnet allows drains the minter's cycle balance until it is frozen or uninstalled, taking ckBTC offline.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L164-183)
```rust
    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;

    let address = state::read_state(|s| runtime.derive_user_address(s, &caller_account));

    let (btc_network, min_confirmations) =
        state::read_state(|s| (s.btc_network, s.min_confirmations));

    let utxos = get_utxos(
        btc_network,
        &address,
        min_confirmations,
        CallSource::Client,
        runtime,
    )
    .await?
    .utxos;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L217-265)
```rust
    if satoshis_to_mint == 0 {
        // We bail out early if there are no UTXOs to avoid creating a new entry
        // in the UTXOs map. If we allowed empty entries, malicious callers
        // could exhaust the canister memory.

        // We get the entire list of UTXOs again with a zero
        // confirmation limit so that we can indicate the approximate
        // wait time to the caller.
        let GetUtxosResponse {
            tip_height,
            mut utxos,
            ..
        } = get_utxos(
            btc_network,
            &address,
            /*min_confirmations=*/ 0,
            CallSource::Client,
            runtime,
        )
        .await?;

        utxos.retain(|u| {
            tip_height
                < u.height
                    .checked_add(min_confirmations)
                    .expect("bug: this shouldn't overflow")
                    .checked_sub(1)
                    .expect("bug: this shouldn't underflow")
        });
        let pending_utxos: Vec<PendingUtxo> = utxos
            .iter()
            .map(|u| PendingUtxo {
                outpoint: u.outpoint.clone(),
                value: u.value,
                confirmations: tip_height - u.height + 1,
            })
            .collect();

        let current_confirmations = pending_utxos.iter().map(|u| u.confirmations).max();

        observe_update_call_latency(0, start_time, runtime.time());

        return Err(UpdateBalanceError::NoNewUtxos {
            current_confirmations,
            required_confirmations: min_confirmations,
            pending_utxos: Some(pending_utxos),
            suspended_utxos: Some(suspended_utxos),
        });
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

**File:** rs/bitcoin/ckbtc/minter/src/management.rs (L129-157)
```rust
    async fn bitcoin_get_utxos<R: CanisterRuntime>(
        now: &mut u64,
        req: GetUtxosRequest,
        source: CallSource,
        runtime: &R,
    ) -> Result<GetUtxosResponse, CallError> {
        match source {
            CallSource::Client => &crate::metrics::GET_UTXOS_CLIENT_CALLS,
            CallSource::Minter => &crate::metrics::GET_UTXOS_MINTER_CALLS,
        }
        .with(|cell| cell.set(cell.get() + 1));
        if let Some(res) = crate::state::read_state(|s| s.get_utxos_cache.get(&req, *now).cloned())
        {
            crate::metrics::GET_UTXOS_CACHE_HITS.with(|cell| cell.set(cell.get() + 1));
            Ok(res)
        } else {
            crate::metrics::GET_UTXOS_CACHE_MISSES.with(|cell| cell.set(cell.get() + 1));
            runtime.get_utxos(&req).await.inspect(|res| {
                *now = runtime.time();
                crate::state::mutate_state(|s| {
                    if s.last_get_utxos_tip_height != Some(res.tip_height) {
                        s.get_utxos_cache.clear();
                        s.last_get_utxos_tip_height = Some(res.tip_height)
                    }
                    s.get_utxos_cache.insert(req, res.clone(), *now)
                })
            })
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```
