### Title
Attacker Can Frontrun Victim's `update_balance` to Cause Persistent `AlreadyProcessing` Errors at Zero Cost - (`rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter's `update_balance` endpoint accepts an optional `owner` principal that can be set to **any** arbitrary principal by any caller. The function then acquires a per-account `balance_update_guard` keyed on the resolved account. Because the guard is held across multiple async inter-canister calls (to the Bitcoin canister and BTC checker), an attacker can continuously call `update_balance(owner = victim)` at near-zero cost to keep the guard occupied, causing every legitimate `update_balance` call from the victim to return `AlreadyProcessing`. This is a direct analog of the DYAD H-04 pattern: an unprivileged caller manipulates a per-account protection mechanism on behalf of another account, blocking that account's legitimate operations.

---

### Finding Description

`update_balance` in the ckBTC minter resolves the target account from the caller-supplied `args.owner` field without any authorization check:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),  // attacker sets this to victim's principal
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;  // guard keyed on victim's account
``` [1](#0-0) 

The `balance_update_guard` inserts the account into `state.update_balance_accounts` and returns `GuardError::AlreadyProcessing` if the account is already present:

```rust
if accounts.contains(&account) {
    return Err(GuardError::AlreadyProcessing);
}
accounts.insert(account);
``` [2](#0-1) 

The guard is held for the entire duration of the async function, which includes at minimum two sequential calls to the Bitcoin canister (`get_utxos` with `min_confirmations`, then `get_utxos` with 0 confirmations when no UTXOs are found): [3](#0-2) [4](#0-3) 

The guard is only released when the `Guard` struct is dropped at the end of the function: [5](#0-4) 

The `AlreadyProcessing` error propagates directly to the caller: [6](#0-5) 

The public DID interface confirms `owner` is optional and callable by anyone: [7](#0-6) 

The same pattern exists in the ckDOGE minter: [8](#0-7) 

---

### Impact Explanation

An attacker can permanently deny a specific victim the ability to mint ckBTC (or ckDOGE) by continuously calling `update_balance(owner = victim_principal)`. Each attacker call:

1. Acquires the `balance_update_guard` for the victim's account.
2. Holds it across two async Bitcoin canister round-trips (the window during which the victim's own call returns `AlreadyProcessing`).
3. Releases the guard, but the attacker immediately re-submits.

Because IC message queues allow the attacker to pre-queue multiple calls, the victim's account can be kept locked indefinitely. The victim's deposited BTC is confirmed on-chain but ckBTC cannot be minted until the attacker stops. This is a targeted, sustained denial-of-service against the deposit/minting flow with no financial loss to the attacker.

---

### Likelihood Explanation

The attack requires only a valid non-anonymous IC principal and knowledge of the victim's principal ID (which is public). No BTC, ckBTC, or special permissions are needed. The attacker pays only the standard IC ingress fee (a few cycles), while the ckBTC minter canister bears the cost of the Bitcoin canister calls. The attack is trivially scriptable and can be sustained indefinitely. The `MAX_CONCURRENT = 100` limit does not protect against a targeted single-account attack. [9](#0-8) 

---

### Recommendation

Restrict `update_balance` so that the `owner` field, when set to a principal other than the caller, is only accepted from authorized callers (e.g., the minter itself or a whitelisted set of principals). The simplest fix is to require `args.owner == None || args.owner == Some(caller)` for unprivileged ingress callers, mirroring the approach used in `retrieve_btc_with_approval` which always uses `ic_cdk::api::msg_caller()` directly without an overridable owner field: [10](#0-9) 

Alternatively, if third-party notification is a desired feature (e.g., a helper canister notifying on behalf of a user), the guard should be keyed on the **caller** rather than the resolved owner, so the attacker's guard does not block the victim's own call.

---

### Proof of Concept

```
// Attacker (any non-anonymous principal) submits in a tight loop:
update_balance({ owner = opt principal "VICTIM_PRINCIPAL_ID"; subaccount = null })

// Victim submits their legitimate call:
update_balance({ owner = null; subaccount = null })
// → Returns: Err(AlreadyProcessing)
// because the attacker's in-flight call holds balance_update_guard for victim's account
// across the async get_utxos() Bitcoin canister call.
```

The attacker's call costs only the IC ingress fee. The victim's BTC deposit is confirmed on-chain but ckBTC minting is blocked for as long as the attacker continues.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L108-116)
```rust
impl From<GuardError> for UpdateBalanceError {
    fn from(e: GuardError) -> Self {
        match e {
            GuardError::AlreadyProcessing => Self::AlreadyProcessing,
            GuardError::TooManyConcurrentRequests => {
                Self::TemporarilyUnavailable("too many concurrent requests".to_string())
            }
        }
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L225-236)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L6-6)
```rust
const MAX_CONCURRENT: usize = 100;
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L45-60)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L696-704)
```text
    // Mints ckBTC for newly deposited UTXOs.
    //
    // If the owner is not set, it defaults to the caller's principal.
    //
    // # Preconditions
    //
    // * The owner deposited some BTC to the address that the
    //   [get_btc_address] endpoint returns.
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```

**File:** rs/dogecoin/ckdoge/minter/ckdoge_minter.did (L525-533)
```text
    // Mints ckDOGE for newly deposited UTXOs.
    //
    // If the owner is not set, it defaults to the caller's principal.
    //
    // # Preconditions
    //
    // * The owner deposited some DOGE to the address that the
    //   [get_doge_address] endpoint returns.
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L247-263)
```rust
) -> Result<RetrieveBtcOk, RetrieveBtcWithApprovalError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
    let caller_account = Account {
        owner: caller,
        subaccount: args.from_subaccount,
    };
    let _guard = retrieve_btc_guard(caller_account)?;
```
