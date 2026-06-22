### Title
`balance_update_guard` Global Slot Exhaustion via Subaccount Flooding Causes DoS for All ckBTC Depositors — (File: `rs/bitcoin/ckbtc/minter/src/guard.rs`)

---

### Summary

The ckBTC minter's `update_balance` endpoint enforces a global cap of `MAX_CONCURRENT = 100` in-flight requests via `balance_update_guard`. Because the guard key is an `Account` (owner + subaccount) and the `args.owner` / `args.subaccount` fields are caller-supplied, a single unprivileged principal can exhaust all 100 slots by submitting 100 concurrent calls with distinct subaccounts. While those calls are suspended awaiting the async `get_utxos` response from the Bitcoin canister, every legitimate depositor receives `TooManyConcurrentRequests` and cannot mint ckBTC. No UTXOs, ckBTC, or privileged access are required.

---

### Finding Description

**Root cause — `rs/bitcoin/ckbtc/minter/src/guard.rs`**

`Guard::new` maintains a single global `BTreeSet<Account>` (`update_balance_accounts`) and rejects any new entry once the set reaches `MAX_CONCURRENT = 100`:

```rust
const MAX_CONCURRENT: usize = 100;

impl<PR: PendingRequests> Guard<PR> {
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {          // ← global cap
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self { account, _marker: PhantomData })
        })
    }
}
``` [1](#0-0) 

**Vulnerable call site — `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`**

The guard is acquired *after* `init_ecdsa_public_key()` and is held across the entire async `get_utxos` inter-canister call:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // ← caller-supplied
    subaccount: args.subaccount,           // ← caller-supplied
};
let _guard = balance_update_guard(caller_account)?;   // slot acquired here

let utxos = get_utxos(btc_network, &address, min_confirmations,
                      CallSource::Client, runtime)
    .await?   // ← guard held across this async suspension
    .utxos;
``` [2](#0-1) 

`UpdateBalanceArgs` exposes both fields to any caller:

```rust
pub struct UpdateBalanceArgs {
    pub owner: Option<Principal>,
    pub subaccount: Option<Subaccount>,
}
``` [3](#0-2) 

The only restriction is that `args.owner` must not equal the minter's own canister ID:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [4](#0-3) 

There is no per-principal rate limit and no restriction on how many distinct subaccounts a single principal may use.

---

### Impact Explanation

An attacker submits 100 concurrent `update_balance` ingress messages, each with a distinct `args.subaccount` (e.g., `[0u8;32]` through `[99u8;32]`). Each message:

1. Passes the mode check (normal operation).
2. Acquires one guard slot for `Account { owner: attacker, subaccount: Some([i;32]) }`.
3. Suspends at `get_utxos(...).await`, holding the slot while waiting for the Bitcoin canister.

With all 100 slots occupied, every subsequent `update_balance` call from any legitimate user returns `Err(UpdateBalanceError::TooManyConcurrentRequests)` — preventing ckBTC minting for the duration of the attack. The attacker can sustain the DoS by re-submitting calls as earlier ones complete (they will all eventually return `NoNewUtxos` since the attacker has no real UTXOs, but the guard is held for the full round-trip to the Bitcoin canister).

The `retrieve_btc_guard` shares the same `MAX_CONCURRENT = 100` constant and the same `Guard::new` implementation, making `retrieve_btc_with_approval` (which accepts a caller-supplied `from_subaccount`) susceptible to the same slot-exhaustion pattern, though that path requires burning ckBTC. [5](#0-4) 

---

### Likelihood Explanation

- **No cost barrier**: The attacker needs no UTXOs, no ckBTC, and no privileged role — only the ability to send ingress messages, which any IC user can do.
- **Single principal sufficient**: 100 distinct subaccounts of one principal fill all slots; creating subaccounts is free.
- **Sustainable**: Each slot is held for the latency of one `get_utxos` call to the Bitcoin canister. The attacker can continuously re-submit to maintain the DoS.
- **Directly reachable**: `update_balance` is a public `#[update]` endpoint on the ckBTC minter canister, callable by any non-anonymous principal. [6](#0-5) 

---

### Recommendation

1. **Add a per-principal concurrent-request cap** inside `Guard::new`: reject a new request if the same `owner` principal already holds `K` slots (e.g., `K = 3`), regardless of subaccount. This prevents a single principal from monopolising the global pool.
2. **Separate the global pool from the per-account deduplication check**: the per-account `AlreadyProcessing` guard is correct and should be kept; only the global `MAX_CONCURRENT` ceiling needs a per-principal sub-limit.
3. Alternatively, **move the guard acquisition to before the `init_ecdsa_public_key` await** so that the slot is held for the shortest possible time, reducing the window an attacker can exploit.

---

### Proof of Concept

```
// Attacker principal: P
// Sends 100 concurrent update_balance ingress messages:
for i in 0..100 {
    update_balance(UpdateBalanceArgs {
        owner: Some(P),
        subaccount: Some([i as u8; 32]),
    });
}

// Each call:
//   1. Passes mode check.
//   2. Acquires guard slot for Account { owner: P, subaccount: Some([i;32]) }.
//   3. Suspends at get_utxos(...).await  ← slot held here.

// While all 100 slots are occupied, any legitimate user calling update_balance
// receives:
//   Err(UpdateBalanceError::TooManyConcurrentRequests)
// and cannot mint ckBTC.

// Attacker re-submits as calls complete (returning NoNewUtxos),
// maintaining the DoS indefinitely at the cost of ingress fees only.
``` [7](#0-6) [2](#0-1)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-12)
```rust
const MAX_CONCURRENT: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L41-61)
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
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L35-41)
```rust
pub struct UpdateBalanceArgs {
    /// The owner of the account on the ledger.
    /// The minter uses the caller principal if the owner is None.
    pub owner: Option<Principal>,
    /// The desired subaccount on the ledger, if any.
    pub subaccount: Option<Subaccount>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L149-151)
```rust
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }
```

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

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```
