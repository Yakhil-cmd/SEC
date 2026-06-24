### Title
Unprivileged Caller Can Grief Any Account's `update_balance` by Specifying Arbitrary `owner` - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

### Summary
The ckBTC minter's `update_balance` endpoint accepts an optional `owner` principal that any unprivileged caller may set to any value. The per-account concurrent-execution guard is keyed on the resolved `owner` account, not the caller. An attacker can therefore continuously acquire the guard for a victim's account, causing every legitimate `update_balance` call from the victim to return `AlreadyProcessing` for the duration of the attacker's in-flight async call. By re-submitting immediately after each call completes, the attacker can sustain an indefinite denial-of-service against the victim's ckBTC minting.

### Finding Description
`update_balance` resolves the target account as:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
``` [1](#0-0) 

There is no check that `args.owner`, when supplied, equals the caller. The guard is backed by a `BTreeSet<Account>` in minter state:

```rust
pub struct PendingBalanceUpdates;
impl PendingRequests for PendingBalanceUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.update_balance_accounts
    }
}
``` [2](#0-1) 

`Guard::new` inserts the account and returns `AlreadyProcessing` if it is already present:

```rust
if accounts.contains(&account) {
    return Err(GuardError::AlreadyProcessing);
}
accounts.insert(account);
``` [3](#0-2) 

The guard is held for the entire async lifetime of `update_balance`, which includes at least one cross-canister call to the Bitcoin canister (`get_utxos`) and potentially multiple calls to the BTC checker canister (up to `MAX_CHECK_TRANSACTION_RETRY = 10`): [4](#0-3) 

The `update_balance` DID interface explicitly documents that `owner` is optional and defaults to the caller, making the parameter publicly accessible to any ingress sender: [5](#0-4) 

### Impact Explanation
A victim who has deposited BTC and is waiting to mint ckBTC calls `update_balance()` (owner = self). An attacker simultaneously calls `update_balance(owner = victim)`. The attacker's call acquires the guard first and holds it across multiple async round-trips to the Bitcoin canister and BTC checker. The victim's call returns `AlreadyProcessing`. The attacker re-submits immediately after each call completes, sustaining the block indefinitely. The victim cannot mint ckBTC from their deposited BTC for as long as the attacker continues. This is directly analogous to the LpToken taint griefing: an external actor triggers a per-account state change that blocks the legitimate owner's operations.

Secondary impact: the `MAX_CONCURRENT = 100` cap on the guard set means an attacker using 100 distinct subaccounts can simultaneously block 100 different victim accounts, or exhaust the global concurrent-request budget and return `TooManyConcurrentRequests` to all other users. [6](#0-5) 

### Likelihood Explanation
The attack requires only the ability to send ingress messages to the ckBTC minter canister — no tokens, no special role, no prior relationship with the victim. The cost per call is the standard IC ingress fee plus cycles consumed by the cross-canister `get_utxos` call. Because `update_balance` for an account with no new UTXOs returns quickly, the attacker must call repeatedly, but the per-call cost remains low. The victim's address is derivable from their principal via `get_btc_address`, which is a public query endpoint, so the attacker can target any known depositor.

### Recommendation
Require that when `args.owner` is explicitly set to a principal other than the caller, the caller must be the owner or a pre-authorized hot-key/delegate. The simplest fix is to key the guard on the **caller** rather than the resolved owner, or to reject calls where `args.owner != Some(caller)` unless the caller is a whitelisted service. Alternatively, rate-limit `update_balance` calls per caller principal to prevent rapid re-submission.

### Proof of Concept

1. Victim `V` deposits BTC to their ckBTC minter address and waits for confirmations.
2. Attacker `A` (any principal) submits:
   ```
   update_balance({ owner = opt V; subaccount = null })
   ```
   This acquires the guard for `Account { owner: V, subaccount: None }` and begins an async `get_utxos` call to the Bitcoin canister.
3. While step 2 is in-flight, victim `V` submits:
   ```
   update_balance({ owner = null; subaccount = null })
   ```
   Resolved account is `Account { owner: V, subaccount: None }`. Guard check finds the account already present → returns `Err(AlreadyProcessing)`.
4. Attacker's call completes (returns `NoNewUtxos` since V's UTXOs belong to V, not A). Guard is dropped.
5. Attacker immediately re-submits step 2. Repeat from step 3.
6. Victim is unable to mint ckBTC for as long as the attacker sustains the loop. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L143-168)
```rust
/// Notifies the ckBTC minter to update the balance of the user subaccount.
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

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-12)
```rust
const MAX_CONCURRENT: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L18-24)
```rust
pub struct PendingBalanceUpdates;

impl PendingRequests for PendingBalanceUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.update_balance_accounts
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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L704-704)
```text
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```
