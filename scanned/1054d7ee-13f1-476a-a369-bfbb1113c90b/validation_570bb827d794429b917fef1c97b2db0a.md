### Title
Unprivileged Caller Can Exhaust `update_balance` Guard Slots to Deny ckBTC Minting for Arbitrary Accounts - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

---

### Summary

The ckBTC minter's `update_balance` endpoint accepts an optional `owner` principal that any non-anonymous caller may set to an **arbitrary** principal. The per-account concurrency guard is keyed on the resolved `Account{owner, subaccount}`, not on the actual caller. Because the guard is held across multiple async inter-canister await points, an attacker can continuously call `update_balance` with a victim's principal as `owner` to hold the victim's guard slot, preventing the victim from minting ckBTC. With 100 concurrent calls targeting 100 distinct accounts, the attacker exhausts `MAX_CONCURRENT = 100` and blocks **all** users from calling `update_balance`.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, the `update_balance` function resolves the target account from caller-supplied arguments:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),  // attacker supplies any principal here
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
``` [1](#0-0) 

The `UpdateBalanceArgs` struct exposes `owner: Option<Principal>` as a public, caller-controlled field with no authorization check: [2](#0-1) 

The guard implementation in `rs/bitcoin/ckbtc/minter/src/guard.rs` inserts the resolved account into a `BTreeSet<Account>` capped at `MAX_CONCURRENT = 100`. Any second call for the same account returns `GuardError::AlreadyProcessing`, and any call when the set is full returns `GuardError::TooManyConcurrentRequests`: [3](#0-2) 

The guard `_guard` is held for the **entire duration** of the async function, which spans multiple inter-canister await points: `init_ecdsa_public_key().await`, `get_utxos(...).await`, `check_transaction(...).await` (up to 10 retries), and `mint_ckbtc(...).await`: [4](#0-3) 

The minter's public Candid interface confirms `owner` is an optional, unauthenticated parameter: [5](#0-4) 

The only caller restriction is that the caller must not be anonymous: [6](#0-5) 

---

### Impact Explanation

**Targeted DoS (single victim):** An attacker calls `update_balance` with `owner = victim_principal`. The guard for `{owner: victim_principal, subaccount: None}` is acquired and held while the minter awaits inter-canister responses (Bitcoin canister, BTC checker, ledger). During this window — which spans multiple consensus rounds — the victim calling `update_balance` for their own account receives `AlreadyProcessing`. The attacker repeats calls continuously to maintain the lock, permanently preventing the victim from converting BTC to ckBTC.

**Global DoS (all users):** The attacker submits 100 concurrent ingress messages to `update_balance`, each targeting a distinct account (e.g., 100 different subaccounts). This fills `update_balance_accounts` to `MAX_CONCURRENT = 100`. Every subsequent `update_balance` call from any user — including legitimate depositors — returns `TemporarilyUnavailable("too many concurrent requests")`. The attacker sustains this by resubmitting calls as old ones complete. [7](#0-6) 

The `CkBtcMinterState` confirms both guard sets are global, shared state: [8](#0-7) 

---

### Likelihood Explanation

- **Entry path is fully open:** Any non-anonymous principal can call `update_balance` with an arbitrary `owner`. No BTC deposit, no ckBTC balance, and no special role is required.
- **Cost is minimal:** The attacker only pays IC ingress message fees (cycles). No Bitcoin is needed.
- **Sustained attack is feasible:** Each `update_balance` call holds its guard for the duration of multiple inter-canister round-trips (seconds to tens of seconds on mainnet). The attacker needs only to keep 100 calls in-flight, which is achievable with a simple script.
- **No threshold or majority required:** A single principal with 100 subaccounts suffices for the global DoS variant.

---

### Recommendation

Restrict the guard key to the **caller's identity**, not the caller-supplied `owner`. Specifically, the guard should be acquired on `Account { owner: caller, subaccount: args.subaccount }` (or on `caller` alone), not on the resolved `caller_account`. Alternatively, require that `args.owner`, if set, must equal the caller's principal (i.e., a caller may only trigger `update_balance` for their own account). This mirrors the fix applied to the dPrime vulnerability: adding a threshold/authorization check before the side-effecting operation.

---

### Proof of Concept

1. Attacker (principal `A`) obtains a non-anonymous identity (trivial on IC).
2. Attacker identifies victim principal `V` (e.g., from on-chain ledger history).
3. Attacker submits 100 concurrent ingress messages to the ckBTC minter's `update_balance` endpoint, each with `owner = V` and a distinct `subaccount` (subaccounts `[0;32]`, `[1;32]`, ..., `[99;32]`).
4. Each call passes the `check_anonymous_caller()` check, resolves `caller_account = {owner: V, subaccount: [i;32]}`, acquires a guard slot, and blocks awaiting `get_utxos` from the Bitcoin canister.
5. `update_balance_accounts.len()` reaches 100.
6. Victim `V` calls `update_balance` → receives `Err(TemporarilyUnavailable("too many concurrent requests"))`.
7. Any other user `U` calls `update_balance` → same error.
8. Attacker resubmits calls as they complete, sustaining the DoS indefinitely. [9](#0-8) [10](#0-9)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L144-183)
```rust
pub async fn update_balance<R: CanisterRuntime>(
    args: UpdateBalanceArgs,
    runtime: &R,
) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }

    // Record start time of method execution for metrics
    let start_time = runtime.time();

    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;

    init_ecdsa_public_key().await;

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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L704-704)
```text
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L444-448)
```rust
    /// Per-account lock for update_balance
    pub update_balance_accounts: BTreeSet<Account>,

    /// Per-account lock for retrieve_btc
    pub retrieve_btc_accounts: BTreeSet<Account>,
```
