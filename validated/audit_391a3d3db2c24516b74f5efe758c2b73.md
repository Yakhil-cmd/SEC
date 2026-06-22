### Title
Unauthenticated Account Lock Squatting in `update_balance` Allows Any Caller to Permanently DoS a Victim's ckBTC Minting - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

### Summary

The ckBTC minter's `update_balance` endpoint accepts an optional `owner` principal that is not validated against the actual caller. The per-account concurrency guard (`balance_update_guard`) is keyed on the caller-supplied `owner`, not on the authenticated caller. Any non-anonymous principal can therefore acquire and continuously re-acquire the guard for any victim's account, causing every `update_balance` call from the legitimate owner to return `AlreadyProcessing` indefinitely.

### Finding Description

**Root cause — unauthenticated owner field used as lock key**

In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` the function resolves the account to lock as:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // ← user-supplied, not validated
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;   // ← lock keyed on victim's principal
``` [1](#0-0) 

The only caller-identity check that exists is that the resolved owner must not equal the minter canister's own principal:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [2](#0-1) 

There is no check that `args.owner` matches `caller`. The mode check that follows is performed on `caller`, not on the resolved owner, so it does not restrict which account can be locked:

```rust
state::read_state(|s| s.mode.is_deposit_available_for(&caller))
``` [3](#0-2) 

**Guard semantics — one holder blocks all others for the same account**

`balance_update_guard` calls `Guard::new`, which inserts the account into a global `BTreeSet`. Any subsequent attempt to acquire the guard for the same account returns `Err(GuardError::AlreadyProcessing)` until the first holder drops it:

```rust
if accounts.contains(&account) {
    return Err(GuardError::AlreadyProcessing);
}
accounts.insert(account);
``` [4](#0-3) 

The guard is held across multiple async inter-canister calls (at minimum two `get_utxos` calls to the Bitcoin canister, plus a ledger mint call), so each attacker invocation holds the lock for a non-trivial wall-clock duration.

**Entry point — publicly reachable update call**

The canister endpoint only rejects anonymous callers:

```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
``` [5](#0-4) 

Any authenticated principal (including a freshly created, zero-balance one) can call `update_balance` with `owner = <victim_principal>`.

**Exploit flow**

1. Attacker submits `update_balance({ owner: Some(victim), subaccount: None })`.
2. Guard for `Account { owner: victim, subaccount: None }` is acquired; the call begins two async Bitcoin-canister round-trips.
3. Victim submits `update_balance({})` (defaulting to their own principal). Guard acquisition fails → `AlreadyProcessing` returned immediately.
4. Attacker's call completes and releases the guard.
5. Attacker immediately re-submits step 1. Because IC message ordering is FIFO per sender, the attacker can pipeline calls so the guard is re-acquired before the victim's next attempt.
6. Repeat indefinitely at negligible cost (no cycles fee beyond the call itself).

The `MAX_CONCURRENT` cap of 100 is per-minter-wide, not per-attacker; a single attacker holding one slot for the victim's account is sufficient to block that victim. [6](#0-5) 

### Impact Explanation

A victim who has deposited BTC and is waiting to mint ckBTC can be prevented from ever successfully calling `update_balance` themselves. While the attacker's calls will eventually process the victim's UTXOs (minting ckBTC to the victim's account), the victim loses the ability to self-initiate the minting step. More critically, if the attacker targets an account with no pending UTXOs (e.g., between deposits), the attacker's calls return `NoNewUtxos` quickly, allowing rapid lock cycling that keeps the victim's account perpetually locked. This constitutes a targeted, sustained service-denial against any specific ckBTC user's deposit flow, achievable by any non-anonymous IC principal at minimal cost.

### Likelihood Explanation

The attack requires no privileged access, no tokens, and no special setup — only a valid (non-anonymous) IC identity. The `owner` field is documented as intentionally accepting any principal (the README explicitly shows calling `update_balance` with a different owner), so the open interface is by design, but the guard keying on that field rather than on the authenticated caller is the unintended consequence. The attack is trivially scriptable and can be sustained indefinitely.

### Recommendation

Key the concurrency guard on the **authenticated caller** rather than on the user-supplied `owner`. If the design intent is to allow third-party minting on behalf of others, the guard should use a composite key of `(caller, target_account)` or simply remove the per-account exclusivity requirement for third-party callers (since the minting itself is idempotent). Alternatively, validate that `args.owner`, when present, matches `caller` unless the caller is a whitelisted service principal.

### Proof of Concept

```
# Attacker identity: any non-anonymous IC principal (e.g., freshly generated)
# Victim: principal "aaaaa-victim-principal-..."

# Step 1: Attacker acquires the victim's update_balance lock
dfx canister --network ic call ckbtc-minter update_balance \
  '(record { owner = opt principal "aaaaa-victim-principal-..."; subaccount = null })'
# → Lock acquired; async Bitcoin canister call in flight

# Step 2 (concurrent): Victim attempts their own update_balance
dfx canister --network ic call ckbtc-minter update_balance '(record {})'
# → Err(AlreadyProcessing)   ← victim is blocked

# Step 3: Attacker loops step 1 immediately after each completion
# → Victim's every subsequent call returns AlreadyProcessing
```

The attacker's call at step 1 holds the guard across at least one `get_utxos` inter-canister call to the Bitcoin canister [7](#0-6)  and, when no new UTXOs exist, a second `get_utxos` call with zero confirmations [8](#0-7) , providing a wide window during which the victim's call is rejected.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L149-151)
```rust
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L159-160)
```rust
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L229-236)
```rust
        } = get_utxos(
            btc_network,
            &address,
            /*min_confirmations=*/ 0,
            CallSource::Client,
            runtime,
        )
        .await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-11)
```rust
const MAX_CONCURRENT: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L48-54)
```rust
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```
