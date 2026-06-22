### Title
Missing `owner` anonymity check in `update_balance` allows minting ckDOGE to the anonymous principal's uncontrolled account - (`rs/dogecoin/ckdoge/minter/src/main.rs`)

---

### Summary

The ckDOGE minter's `update_balance` endpoint validates that the **caller** (`msg_caller`) is non-anonymous but never validates that the **`owner` field** in `UpdateBalanceArgs` is non-anonymous. A non-anonymous attacker can pass `owner = Some(Principal::anonymous())`, causing the minter to mint ckDOGE to the anonymous principal's ledger account — an account no one controls. The deposited DOGE is permanently locked in the minter's UTXO set and the minted ckDOGE is permanently inaccessible, breaking the 1:1 backing invariant.

---

### Finding Description

**Entry point:** `update_balance` in `rs/dogecoin/ckdoge/minter/src/main.rs`

```
update_balance(UpdateBalanceArgs { owner: Some(Principal::anonymous()), subaccount: None })
```

**Step 1 — `check_anonymous_caller` only checks `msg_caller`, not `args.owner`:** [1](#0-0) [2](#0-1) 

The guard passes because the attacker's `msg_caller` is non-anonymous. The `args.owner` field is never inspected here.

**Step 2 — The shared `update_balance` logic resolves `caller_account` using `args.owner` directly:** [3](#0-2) 

The only check on the resolved owner is `args.owner.unwrap_or(caller) == runtime.id()` (minter self-deposit guard). There is **no** `assert_ne!(owner, Principal::anonymous())` check here, unlike `get_doge_address` which does have it: [4](#0-3) 

**Step 3 — The minter mints ckDOGE to the anonymous account via `icrc1_transfer`:** [5](#0-4) [6](#0-5) 

The mint call uses `icrc1_transfer` (from the minting account to `caller_account`). The ICRC-1 ledger **does not** block `icrc1_transfer` to the anonymous principal — this is explicitly confirmed by the `test_anonymous_transfers` test: [7](#0-6) 

Note: `icrc152_mint` does block anonymous targets, but the minter uses `icrc1_transfer`, not `icrc152_mint`.

---

### Impact Explanation

1. An attacker deposits DOGE to the Dogecoin address derived from the anonymous principal's account (derivable off-chain from the minter's public ECDSA key using the deterministic derivation path in `rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs` lines 44–53).
2. They call `update_balance` with `owner = Some(Principal::anonymous())` from any non-anonymous principal.
3. The minter mints ckDOGE to `Account { owner: anonymous, subaccount: None }`.
4. The deposited DOGE is permanently locked in the minter's UTXO set (no one can burn the anonymous account's ckDOGE to retrieve it).
5. ckDOGE total supply increases without a corresponding redeemable DOGE, permanently breaking the 1:1 backing invariant. [8](#0-7) 

---

### Likelihood Explanation

- Requires no privileged access; any non-anonymous principal can call `update_balance`.
- The attacker must sacrifice their own DOGE (self-funded griefing attack).
- No financial gain for the attacker, but the protocol invariant is permanently violated proportional to the amount sacrificed.
- Likelihood of accidental triggering is low; deliberate exploitation is straightforward.

---

### Recommendation

Add an explicit non-anonymous check on the resolved `owner` inside `update_balance`, mirroring the check already present in `get_doge_address`:

```rust
let owner = args.owner.unwrap_or(caller);
assert_ne!(
    owner,
    Principal::anonymous(),
    "owner must be non-anonymous"
);
```

This should be added either in the ckDOGE minter's `update_balance` wrapper (before delegating to the shared logic) or in the shared `ic_ckbtc_minter::updates::update_balance::update_balance` function itself.

---

### Proof of Concept

State-machine test outline:

```rust
// 1. Compute the Dogecoin address for the anonymous principal's account
let anon_doge_address = minter.get_doge_address(
    non_anon_caller,
    &GetDogeAddressArgs { owner: Some(Principal::anonymous()), subaccount: None },
); // This will trap — compute off-chain instead using the public ECDSA key

// 2. Deposit DOGE to that address via the Dogecoin network simulation

// 3. Mine enough blocks for confirmations

// 4. Call update_balance with owner = anonymous from a non-anonymous caller
let result = minter.update_balance(
    non_anon_caller,  // msg_caller is non-anonymous → passes check_anonymous_caller
    &UpdateBalanceArgs {
        owner: Some(Principal::anonymous()),
        subaccount: None,
    },
);

// 5. Assert: result is Ok(Minted { ... }) — ckDOGE minted to anonymous account
// 6. Assert: ledger balance of anonymous account > 0
// 7. Assert: no one can retrieve the DOGE (anonymous account cannot call retrieve_doge_with_approval)
``` [1](#0-0) [9](#0-8)

### Citations

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L89-96)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(
        ic_ckbtc_minter::updates::update_balance::update_balance(args, &DOGECOIN_CANISTER_RUNTIME)
            .await,
    )
}
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L145-149)
```rust
fn check_anonymous_caller() {
    if ic_cdk::api::msg_caller() == candid::Principal::anonymous() {
        panic!("anonymous caller not allowed")
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L148-167)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L339-341)
```rust
        match runtime
            .mint_ckbtc(amount, caller_account, crate::memo::encode(&memo).into())
            .await
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L458-480)
```rust
/// Mint an amount of ckBTC to an Account.
pub async fn mint(amount: u64, to: Account, memo: Memo) -> Result<u64, UpdateBalanceError> {
    debug_assert!(memo.0.len() <= crate::CKBTC_LEDGER_MEMO_SIZE as usize);
    let client = ICRC1Client {
        runtime: CdkRuntime,
        ledger_canister_id: state::read_state(|s| s.ledger_id.get().into()),
    };
    let block_index = client
        .transfer(TransferArg {
            from_subaccount: None,
            to,
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(amount),
        })
        .await
        .map_err(|(code, msg)| {
            UpdateBalanceError::TemporarilyUnavailable(format!(
                "cannot mint ckbtc: {msg} (reject_code = {code})"
            ))
        })??;
    Ok(block_index.0.to_u64().expect("nat does not fit into u64"))
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L12-18)
```rust
    let owner = owner.unwrap_or_else(ic_cdk::api::msg_caller);
    let account = Account { owner, subaccount };
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L44-53)
```rust
pub fn derivation_path(account: &Account) -> Vec<Vec<u8>> {
    const SCHEMA_V1: u8 = 1;
    const PREFIX: [u8; 4] = *b"doge";

    vec![
        vec![SCHEMA_V1],
        PREFIX.to_vec(),
        account.owner.as_slice().to_vec(),
        account.effective_subaccount().to_vec(),
    ]
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L673-679)
```rust
    // Transfer to the account of the anonymous principal
    println!("transferring to the account of the anonymous principal");
    transfer(&env, canister_id, p1.0, anon.0, TRANSFER_AMOUNT).expect("transfer failed");

    // Transfer from the account of the anonymous principal
    println!("transferring from the account of the anonymous principal");
    transfer(&env, canister_id, anon.0, p1.0, TRANSFER_AMOUNT).expect("transfer failed");
```
