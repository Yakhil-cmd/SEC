### Title
ckBTC Minter `get_btc_address` Accepts Minter's Own Principal as `owner`, Enabling Permanently Unrecoverable BTC Deposits — (File: `rs/bitcoin/ckbtc/minter/src/updates/get_btc_address.rs`)

---

### Summary

The ckBTC minter's `get_btc_address` endpoint accepts the minter's own principal as the `owner` parameter without rejection. This allows any unprivileged caller to obtain the minter's own BTC deposit address. BTC sent to that address becomes permanently unrecoverable: `update_balance` hard-traps when `owner == minter_principal`, and no sweep/recovery endpoint exists. This is the direct IC analog of the PSP22Wrapper `deposit_for(wrapper_address)` stuck-token bug.

---

### Finding Description

`get_btc_address` resolves the `owner` field and only validates that it is not the anonymous principal:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/get_btc_address.rs  lines 29-47
pub async fn get_btc_address(args: GetBtcAddressArgs) -> String {
    let owner = args.owner.unwrap_or_else(ic_cdk::api::msg_caller);
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );
    // ... derives and returns the BTC address for Account { owner, subaccount }
}
```

There is **no check** that `owner != ic_cdk::api::canister_self()` (the minter's own principal). [1](#0-0) 

The minter's own main BTC deposit address is derived from `Account { owner: canister_self(), subaccount: None }`:

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 1797-1807
fn derive_minter_address(&self, state: &CkBtcMinterState) -> BitcoinAddress {
    let main_account = Account {
        owner: ic_cdk::api::canister_self(),
        subaccount: None,
    };
    address::account_to_bitcoin_address(ecdsa_public_key, &main_account)
}
``` [2](#0-1) 

This is **the same address** that `get_btc_address(owner = minter_principal, subaccount = None)` returns. Any caller can therefore obtain the minter's own BTC deposit address and send real BTC to it.

When `update_balance` is subsequently called with `owner = minter_principal`, the minter hard-traps unconditionally:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs  lines 148-151
let caller = runtime.caller();
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [3](#0-2) 

The trap is confirmed by the existing test `test_illegal_caller`:

```rust
// rs/bitcoin/ckbtc/minter/tests/tests.rs  lines 694-706
// update_balance with minter's principal as target
let update_balance_args = UpdateBalanceArgs {
    owner: Some(Principal::from_str(&minter_id.get().to_string()).unwrap()),
    subaccount: None,
};
// This call should panick
let res = env.execute_ingress_as(..., "update_balance", ...).unwrap();
assert!(res.is_err());
``` [4](#0-3) 

There is no alternative sweep or recovery endpoint in the minter for BTC held at its own deposit address. The `retrieve_btc` / `retrieve_btc_with_approval` flows only guard against the minter's address being used as a **withdrawal destination** (the `if args.address == main_address_str` trap), not as a deposit source. [5](#0-4) 

---

### Impact Explanation

Any BTC sent to the address returned by `get_btc_address(owner = minter_principal)` is permanently locked:

- `update_balance(owner = minter_principal)` traps — no ckBTC is ever minted for those UTXOs.
- No sweep/recovery endpoint exists in the minter.
- The minter holds the ECDSA key for that address, so the BTC is not lost to the Bitcoin network, but it is inaccessible without a governance-approved canister upgrade that adds a recovery path.

This breaks the 1:1 BTC↔ckBTC conservation invariant: BTC is absorbed by the minter's own deposit address and permanently excluded from the ckBTC supply, with no on-chain mechanism to reclaim it.

---

### Likelihood Explanation

The minter's principal is publicly known (mainnet: `mqygn-kiaaa-aaaar-qaadq-cai`). The BTC deposit address for `Account { owner: minter_principal, subaccount: None }` is deterministically computable from the minter's ECDSA public key, which is also public. Any user who mistakenly passes the minter's principal as the `owner` in `get_btc_address` — or any malicious actor who deliberately sends BTC to that address — triggers the stuck-funds condition. The scenario is realistic: integrators building on top of the minter API may pass the minter's principal as a proxy/relay owner, or a user may confuse the minter principal with their own.

---

### Recommendation

Add a guard in `get_btc_address` (and symmetrically in `get_known_utxos`) that rejects calls where the resolved `owner` equals the minter's own principal:

```rust
let owner = args.owner.unwrap_or_else(ic_cdk::api::msg_caller);
assert_ne!(owner, Principal::anonymous(), "the owner must be non-anonymous");
assert_ne!(owner, ic_cdk::api::canister_self(), "the owner must not be the minter");
```

This mirrors the guard already present in `update_balance` and is consistent with the existing `retrieve_btc` guard that rejects the minter's own BTC address as a withdrawal target.

---

### Proof of Concept

1. Attacker (any non-anonymous principal) calls:
   ```
   get_btc_address(record { owner = opt principal "mqygn-kiaaa-aaaar-qaadq-cai"; subaccount = null })
   ```
   The call succeeds and returns the minter's own P2WPKH BTC address (e.g., `bc1q...`).

2. Attacker sends any amount of BTC to that address on the Bitcoin network.

3. After sufficient confirmations, attacker (or anyone) calls:
   ```
   update_balance(record { owner = opt principal "mqygn-kiaaa-aaaar-qaadq-cai"; subaccount = null })
   ```
   The minter traps with `"cannot update minter's balance"`. The UTXOs are never processed.

4. The BTC remains locked at the minter's own deposit address indefinitely. The ckBTC total supply is lower than the total BTC held by the minter, breaking the 1:1 conservation invariant with no on-chain recovery path.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/get_btc_address.rs (L29-47)
```rust
pub async fn get_btc_address(args: GetBtcAddressArgs) -> String {
    let owner = args.owner.unwrap_or_else(ic_cdk::api::msg_caller);
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );

    init_ecdsa_public_key().await;

    read_state(|s| {
        account_to_p2wpkh_address_from_state(
            s,
            &Account {
                owner,
                subaccount: args.subaccount,
            },
        )
    })
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1797-1807)
```rust
    fn derive_minter_address(&self, state: &CkBtcMinterState) -> BitcoinAddress {
        let main_account = Account {
            owner: ic_cdk::api::canister_self(),
            subaccount: None,
        };
        let ecdsa_public_key = state
            .ecdsa_public_key
            .as_ref()
            .expect("bug: the ECDSA public key must be initialized");
        address::account_to_bitcoin_address(ecdsa_public_key, &main_account)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L148-151)
```rust
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L694-706)
```rust
    // update_balance with minter's principal as target
    let update_balance_args = UpdateBalanceArgs {
        owner: Some(Principal::from_str(&minter_id.get().to_string()).unwrap()),
        subaccount: None,
    };
    // This call should panick
    let res = env.execute_ingress_as(
        authorized_principal.into(),
        minter_id,
        "update_balance",
        Encode!(&update_balance_args).unwrap(),
    );
    assert!(res.is_err());
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L156-160)
```rust
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```
