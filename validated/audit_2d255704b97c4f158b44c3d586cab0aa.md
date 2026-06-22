### Title
Missing Anonymous Principal Validation in `update_balance` Allows ckBTC Minting to Anonymous Account - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter's `update_balance` endpoint accepts an optional `owner` principal parameter. While the caller is checked to be non-anonymous, there is no validation that the explicitly supplied `args.owner` is not `Principal::anonymous()`. This allows a non-anonymous caller to trigger minting of ckBTC directly into the anonymous principal's ledger account — an account that any unsigned (anonymous) ingress call can spend from.

---

### Finding Description

The `update_balance` endpoint in the ckBTC minter canister accepts `UpdateBalanceArgs { owner: Option<Principal>, subaccount: Option<Subaccount> }`. When `owner` is `Some(...)`, the minter uses that principal as the mint destination instead of the caller.

In `rs/bitcoin/ckbtc/minter/src/main.rs`, the outer handler calls `check_anonymous_caller()` before dispatching:

```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();   // only checks msg_caller(), NOT args.owner
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
``` [1](#0-0) 

Inside `update_balance`, the resolved owner is used directly without any anonymous check:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // args.owner = Some(Principal::anonymous()) passes through
    subaccount: args.subaccount,
};
``` [2](#0-1) 

The only guard inside `update_balance` checks whether the resolved owner equals the minter's own canister ID — not whether it is anonymous:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [3](#0-2) 

By contrast, `get_btc_address` — the sibling endpoint — explicitly rejects the anonymous principal as owner:

```rust
pub async fn get_btc_address(args: GetBtcAddressArgs) -> String {
    let owner = args.owner.unwrap_or_else(ic_cdk::api::msg_caller);
    assert_ne!(owner, Principal::anonymous(), "the owner must be non-anonymous");
``` [4](#0-3) 

The DID interface confirms `update_balance` accepts an optional owner with no stated restriction on anonymous: [5](#0-4) 

---

### Impact Explanation

1. An attacker computes the BTC deposit address for `Principal::anonymous()` off-chain (the minter's threshold ECDSA public key is public and the derivation path is deterministic).
2. The attacker sends BTC to that address and waits for confirmations.
3. The attacker calls `update_balance` with `owner: Some(Principal::anonymous())` from any non-anonymous principal.
4. The minter mints ckBTC to the anonymous principal's ledger account.
5. Because the IC allows unsigned (anonymous) ingress calls, **any party** can call `icrc1_transfer` as the anonymous principal and drain those ckBTC — confirmed by the ledger's own test suite which shows anonymous-principal transfers succeed: [6](#0-5) 

The net result is a **chain-fusion mint bug**: ckBTC is minted to an account that is effectively a public pool, breaking the 1:1 BTC↔ckBTC conservation guarantee for the depositor and enabling theft of the minted tokens.

---

### Likelihood Explanation

The attacker-controlled entry path is fully reachable by any unprivileged ingress sender:
- No privileged role or key is required.
- The BTC address for the anonymous principal can be derived off-chain from the publicly known minter ECDSA public key.
- The `update_balance` call itself requires only a non-anonymous caller, which any user satisfies.
- The inconsistency with `get_btc_address` (which does reject anonymous) means this gap is non-obvious and unlikely to be caught by casual review.

Likelihood is **medium**: it requires the attacker to fund the attack with BTC, but the payoff (stealing minted ckBTC from the anonymous account) can exceed the cost if the anonymous account accumulates deposits from multiple sources.

---

### Recommendation

Add the same anonymous-principal guard to `update_balance` that already exists in `get_btc_address`. Inside `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, after resolving the owner, add:

```rust
let resolved_owner = args.owner.unwrap_or(caller);
if resolved_owner == Principal::anonymous() {
    ic_cdk::trap("the owner must be non-anonymous");
}
```

This mirrors the existing pattern in `get_btc_address`: [7](#0-6) 

---

### Proof of Concept

```
// Attacker (non-anonymous principal P) calls:
update_balance({
    owner: Some(Principal::anonymous()),
    subaccount: None,
})

// check_anonymous_caller() passes because msg_caller() == P (non-anonymous)
// Inside update_balance:
//   args.owner.unwrap_or(caller) == Principal::anonymous()  ← no rejection
//   caller_account = Account { owner: Principal::anonymous(), subaccount: None }
//   Minter mints ckBTC to anonymous principal's account

// Any party then drains the anonymous account:
icrc1_transfer({
    from_subaccount: None,
    to: Account { owner: attacker_principal, subaccount: None },
    amount: <minted_amount>,
    ...
})
// Sent as an unsigned (anonymous) ingress call — succeeds per ledger semantics
```

The root cause is at: [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L144-167)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/get_btc_address.rs (L29-35)
```rust
pub async fn get_btc_address(args: GetBtcAddressArgs) -> String {
    let owner = args.owner.unwrap_or_else(ic_cdk::api::msg_caller);
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L652-686)
```rust
pub fn test_anonymous_transfers<T>(ledger_wasm: Vec<u8>, encode_init_args: fn(InitArgs) -> T)
where
    T: CandidType,
{
    const INITIAL_BALANCE: u64 = 10_000_000;
    const TRANSFER_AMOUNT: u64 = 1_000_000;
    let p1 = PrincipalId::new_user_test_id(1);
    let anon = PrincipalId::new_anonymous();
    let (env, canister_id) = setup(
        ledger_wasm,
        encode_init_args,
        vec![
            (Account::from(p1.0), INITIAL_BALANCE),
            (Account::from(anon.0), INITIAL_BALANCE),
        ],
    );

    assert_eq!(INITIAL_BALANCE * 2, total_supply(&env, canister_id));
    assert_eq!(INITIAL_BALANCE, balance_of(&env, canister_id, p1.0));
    assert_eq!(INITIAL_BALANCE, balance_of(&env, canister_id, anon.0));

    // Transfer to the account of the anonymous principal
    println!("transferring to the account of the anonymous principal");
    transfer(&env, canister_id, p1.0, anon.0, TRANSFER_AMOUNT).expect("transfer failed");

    // Transfer from the account of the anonymous principal
    println!("transferring from the account of the anonymous principal");
    transfer(&env, canister_id, anon.0, p1.0, TRANSFER_AMOUNT).expect("transfer failed");

    assert_eq!(
        INITIAL_BALANCE * 2 - FEE * 2,
        total_supply(&env, canister_id)
    );
    assert_eq!(INITIAL_BALANCE - FEE, balance_of(&env, canister_id, p1.0));
    assert_eq!(INITIAL_BALANCE - FEE, balance_of(&env, canister_id, anon.0));
```
