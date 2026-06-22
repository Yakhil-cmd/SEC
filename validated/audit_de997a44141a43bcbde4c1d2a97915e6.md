### Title
ckBTC Minter Mode Check in `retrieve_btc()` Blocks Completion of Already-Committed Withdrawals — (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The ckBTC minter's legacy two-phase withdrawal flow (`get_withdrawal_account()` → ledger transfer → `retrieve_btc()`) has an asymmetric mode guard: `get_withdrawal_account()` performs no mode check, but `retrieve_btc()` unconditionally checks `is_withdrawal_available_for()` before inspecting the caller's balance. If the minter's `Mode` is changed to `RestrictedTo` or `ReadOnly` after a user has already transferred ckBTC into the withdrawal subaccount but before they call `retrieve_btc()`, the user's funds are stranded in a minter-owned subaccount with no user-accessible recovery path.

---

### Finding Description

The ckBTC minter exposes two withdrawal paths:

**New flow** (`retrieve_btc_with_approval`): single atomic call; mode check is appropriate because no funds are pre-committed.

**Old flow** (three steps):
1. `get_withdrawal_account()` — returns a deterministic subaccount `{owner: minter_canister, subaccount: sha256("ckbtc" ‖ caller ‖ 0)}`. **No mode check.**
2. User calls `icrc1_transfer` on the ckBTC ledger to deposit funds into that subaccount. **No minter mode check** (ledger operation).
3. `retrieve_btc()` — **mode check fires first**, before any balance inspection. [1](#0-0) 

```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;   // ← blocks here
```

The balance check only comes later: [2](#0-1) 

The `Mode` enum and `is_withdrawal_available_for` logic: [3](#0-2) 

`ReadOnly` and `RestrictedTo` both return an error for non-allowlisted callers. `get_withdrawal_account()` has no such guard: [4](#0-3) 

The withdrawal subaccount is owned by the minter canister, not the user. Once ckBTC is transferred there, the user has no ICRC-1 authority to move it back; only the minter can burn or transfer from that account.

---

### Impact Explanation

A user who has completed steps 1–2 (transferred ckBTC to the withdrawal subaccount) but has not yet called `retrieve_btc()` will have their ckBTC permanently locked in the minter-owned subaccount if the mode is changed to `RestrictedTo([admin])` or `ReadOnly` before step 3. There is no user-callable endpoint to reclaim funds from the withdrawal subaccount. The minter provides no automatic reimbursement for funds stranded at this stage. Under a permanent `RestrictedTo` mode, the loss is irreversible without a governance upgrade.

**Impact class**: Ledger conservation bug — user ckBTC is provably locked with no user-accessible exit.

---

### Likelihood Explanation

The scenario requires a mode change between a user's ledger transfer and their `retrieve_btc()` call. Mode changes are governance-controlled upgrades, which happen during planned maintenance windows. The old withdrawal flow is documented and still supported. During any maintenance window where the mode is set to `RestrictedTo` or `ReadOnly`, any user mid-flow loses access to their committed funds for the duration — and permanently if the mode is never restored. The likelihood is **low-to-medium**: mode changes are infrequent, but the old flow is still the documented path in the README. [5](#0-4) 

---

### Recommendation

Apply the same fix pattern recommended in the external report: distinguish between *initiating* a new withdrawal and *completing* an already-committed one.

Concretely, add a balance-first check in `retrieve_btc()`: if the caller's withdrawal subaccount already holds funds ≥ `args.amount`, allow the call to proceed regardless of the current mode (the funds are already committed). The mode check should only block callers who have not yet pre-funded the withdrawal account. Alternatively, add a mode check to `get_withdrawal_account()` so that users cannot enter the pre-funding phase when the minter is restricted, making the two phases consistent.

---

### Proof of Concept

1. User calls `get_withdrawal_account()` → receives `{owner: minter, subaccount: S}`.
2. User calls `icrc1_transfer({to: {owner: minter, subaccount: S}, amount: 1_000_000})` on the ckBTC ledger. Transfer succeeds; ckBTC is now in the minter-owned subaccount.
3. Governance upgrades the minter with `UpgradeArgs { mode: Some(Mode::RestrictedTo(vec![admin_principal])), .. }`.
4. User calls `retrieve_btc(RetrieveBtcArgs { amount: 1_000_000, address: "bc1..." })`.
5. `is_withdrawal_available_for(&caller)` returns `Err("BTC withdrawals are temporarily restricted")` at line 152–153 of `retrieve_btc.rs`.
6. The call returns `Err(TemporarilyUnavailable(...))`. The user's 1,000,000 satoshi of ckBTC remain in the minter subaccount. The user has no ICRC-1 authority over that subaccount and no minter endpoint to recover the funds. [6](#0-5) [7](#0-6) [4](#0-3)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-153)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L181-184)
```rust
    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L343-388)
```rust
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    /// Only the specified principals can modify the minter's state.
    RestrictedTo(Vec<Principal>),
    /// Only the specified principals can deposit BTC.
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    /// No restrictions on the minter interactions.
    GeneralAvailability,
}

impl Mode {
    /// Returns Ok if the specified principal can convert BTC to ckBTC.
    pub fn is_deposit_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("access to the minter is temporarily restricted".to_string());
                }
                Ok(())
            }
            Self::DepositsRestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC deposits are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }

    /// Returns Ok if the specified principal can convert ckBTC to BTC.
    pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability | Self::DepositsRestrictedTo(_) => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC withdrawals are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/get_withdrawal_account.rs (L8-21)
```rust
pub async fn get_withdrawal_account() -> Account {
    let caller = PrincipalId(ic_cdk::api::msg_caller());
    init_ecdsa_public_key().await;
    let ck_btc_principal = ic_cdk::api::canister_self();
    let caller_subaccount: Subaccount = compute_subaccount(caller, 0);
    // Check that the computed subaccount doesn't collide with minting account.
    if &caller_subaccount == DEFAULT_SUBACCOUNT {
        panic!("Subaccount collision with principal {caller}. Please contact DFINITY support.");
    }
    Account {
        owner: ck_btc_principal,
        subaccount: Some(caller_subaccount),
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/README.adoc (L113-165)
```text
=== ckBTC to Bitcoin (old flow)
```
 ┌────┐                       ┌──────┐    ┌────────────┐    ┌───────────────┐
 │User│                       │Minter│    │ckBTC ledger│    │Bitcoin Network│
 └─┬──┘                       └──┬───┘    └─────┬──────┘    └───────┬───────┘
   │                             │              │                   │
   │  get_withdrawal_account()   │              │                   │
   │────────────────────────────>│              │                   │
   │                             │              │                   │
   │       account               │              │                   │
   │<────────────────────────────│              │                   │
   │                             │              │                   │
   │         icrc1_transfer(account)            │                   │
   │───────────────────────────────────────────>│                   │
   │                             │              │                   │
   │retrieve_btc(address,amount) │              │                   │
   │────────────────────────────>│              │                   │
   │                             │              │                   │
   │                             │  Send BTC to withdrawal address  │
   │                             │─────────────────────────────────>│
 ┌─┴──┐                       ┌──┴───┐    ┌─────┴──────┐    ┌───────┴───────┐
 │User│                       │Minter│    │ckBTC ledger│    │Bitcoin Network│
 └────┘                       └──────┘    └────────────┘    └───────────────┘
```

1. Obtain the withdrawal address and store it in a variable.
+
----
withdrawal_address=$(dfx canister --network ic call minter get_withdrawal_account)
----
+
2. Clean the output of the previous command to get the desired format:
+
----
cleaned_withdrawal_address="$(printf "%s\n" "$withdrawal_address" | sed -re 's/^\(|,|\)$//g')"
----
+
3. Transfer the ckBTCs you want to convert, to *cleaned_withdrawal_address* on the ckBTC ledger.
   Replace AMOUNT with the amount that you want to convert.
+
----
dfx canister --network ic call ledger icrc1_transfer "(record {from=null; to=$cleaned_withdrawal_address; amount=AMOUNT; fee=null; memo=null; created_at_time=null;})"
----
+
4. Call the `retrieve_btc` endpoint with the desired BTC destination address where you want to receive your Bitcoin.
   Replace BTC_ADDRESS with your BTC address (the minter supports all address formats).
   Replace AMOUNT with the amount that you transferred minus the transfer fee of 0.0000001 ckBTC (the equivalent of 10 Satoshi).
+
----
dfx canister --network ic call minter retrieve_btc "(record {address=\"BTC_ADDRESS\"; amount=AMOUNT})"
----

You now have your BTC back on the Bitcoin network (caution: transaction finalization may take a while).
```
