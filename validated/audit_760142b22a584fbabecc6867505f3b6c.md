### Title
Unprivileged Caller Can Exhaust the `update_balance` Concurrent-Request Guard, Blocking All ckBTC Deposits — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter's `update_balance` endpoint accepts an optional `owner` field that can be set to **any** principal by any caller. The concurrent-request guard (`balance_update_guard`) is keyed on the resolved account and is globally capped at `MAX_CONCURRENT = 100`. An attacker with no ckBTC or BTC can call `update_balance` for 100 distinct victim accounts, holding each guard slot open across multiple inter-canister call rounds, and thereby deny all legitimate `update_balance` calls with `TooManyConcurrentRequests`. This is a direct analog to the Velodrome `rewards` array exhaustion: a bounded shared collection is polluted with attacker-chosen entries at negligible cost, blocking legitimate participants.

---

### Finding Description

**Root cause — guard keyed on attacker-supplied account:**

In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, the target account is resolved as:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
``` [1](#0-0) 

The `owner` field is declared optional in the public DID interface:

```
update_balance : (record { owner: opt principal; subaccount : opt blob }) -> ...
``` [2](#0-1) 

The only restriction is that the resolved owner must not equal the minter's own canister ID:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [3](#0-2) 

**Root cause — bounded global guard set:**

In `rs/bitcoin/ckbtc/minter/src/guard.rs`, `Guard::new` inserts the account into `CkBtcMinterState::update_balance_accounts` (a `BTreeSet<Account>`) and rejects new requests once the set reaches `MAX_CONCURRENT = 100`:

```rust
const MAX_CONCURRENT: usize = 100;
...
if accounts.len() >= MAX_CONCURRENT {
    return Err(GuardError::TooManyConcurrentRequests);
}
accounts.insert(account);
``` [4](#0-3) 

The guard is held for the **entire async lifetime** of the call. The function makes at least one inter-canister call to the Bitcoin canister (`get_utxos`), which spans multiple IC execution rounds:

```rust
let utxos = get_utxos(btc_network, &address, min_confirmations, CallSource::Client, runtime)
    .await?
    .utxos;
``` [5](#0-4) 

**Attack path:**

An attacker submits 100 ingress messages to `update_balance`, each with a distinct `owner = Some(principal_i)` for `i ∈ 0..100`. Each call:
1. Resolves `caller_account` to the attacker-chosen principal.
2. Acquires a guard slot in `update_balance_accounts`.
3. Suspends at `get_utxos(...).await` while the Bitcoin canister responds.

During this window — which spans multiple rounds — `update_balance_accounts.len() == 100`. Any legitimate call returns `GuardError::TooManyConcurrentRequests`, surfaced as `UpdateBalanceError::TemporarilyUnavailable`. The attacker re-submits calls as guards are released, sustaining the DoS at the cost of IC ingress fees only.

The developers were already aware of a related memory-exhaustion risk from the `owner` field (the comment at line 219 explicitly notes "malicious callers could exhaust the canister memory"), but the concurrent-guard exhaustion vector was not addressed:

```rust
// We bail out early if there are no UTXOs to avoid creating a new entry
// in the UTXOs map. If we allowed empty entries, malicious callers
// could exhaust the canister memory.
``` [6](#0-5) 

---

### Impact Explanation

All ckBTC deposits are blocked. Users who have sent BTC to their minter-derived address cannot call `update_balance` to mint ckBTC. The `update_balance_accounts` set is a global resource shared across all users; exhausting it with 100 attacker-chosen accounts denies service to every legitimate depositor. The DoS is sustained as long as the attacker keeps re-submitting calls, which costs only IC ingress cycles.

---

### Likelihood Explanation

The attack requires no ckBTC, no BTC, and no privileged role — only the ability to send ingress messages to the ckBTC minter canister, which is open to any principal. The cost is 100 concurrent ingress calls, each costing a small number of cycles. The Bitcoin canister's `get_utxos` response latency (typically several seconds per round-trip) gives the attacker a comfortable window to maintain the guard set at capacity. The `retrieve_btc_accounts` guard shares the same `MAX_CONCURRENT = 100` limit but requires burning real ckBTC, making it a higher-cost attack; the `update_balance` path has no such cost barrier.

---

### Recommendation

1. **Restrict `owner` to the caller's own principal.** Remove the ability for a caller to specify an arbitrary `owner`. If cross-account minting is required, gate it behind an explicit allowlist or require the target account's signature.
2. **Alternatively, key the guard on the caller, not the resolved account.** This prevents one caller from occupying slots for accounts they do not control.
3. **Increase `MAX_CONCURRENT` or make it per-caller.** A per-caller limit of, say, 5 concurrent requests would prevent a single attacker from exhausting the global pool.

---

### Proof of Concept

```
// Attacker submits 100 ingress messages to the ckBTC minter:
for i in 0..100 {
    update_balance({
        owner: Some(principal_from_u64(i)),  // 100 distinct victim accounts
        subaccount: None,
    });
    // Each call holds a guard slot while awaiting get_utxos() from the Bitcoin canister.
}

// Now update_balance_accounts.len() == 100.
// Any legitimate user calling update_balance() receives:
//   Err(UpdateBalanceError::TemporarilyUnavailable("too many concurrent requests"))
// The attacker re-submits as guards expire, sustaining the DoS at negligible cost.
```

The guard set is defined in: [7](#0-6) 

The guard enforcement is in: [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L149-151)
```rust
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L164-168)
```rust
    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L175-183)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L217-221)
```rust
    if satoshis_to_mint == 0 {
        // We bail out early if there are no UTXOs to avoid creating a new entry
        // in the UTXOs map. If we allowed empty entries, malicious callers
        // could exhaust the canister memory.

```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L704-704)
```text
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-61)
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
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L444-448)
```rust
    /// Per-account lock for update_balance
    pub update_balance_accounts: BTreeSet<Account>,

    /// Per-account lock for retrieve_btc
    pub retrieve_btc_accounts: BTreeSet<Account>,
```
