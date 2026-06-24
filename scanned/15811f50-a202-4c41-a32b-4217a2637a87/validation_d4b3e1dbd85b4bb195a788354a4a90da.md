### Title
`get_minter_info()` Does Not Expose Operational `Mode`, Misleading Callers About Deposit/Withdrawal Availability - (File: `rs/bitcoin/ckbtc/minter/src/main.rs`, `rs/dogecoin/ckdoge/minter/src/main.rs`)

---

### Summary

The ckBTC and ckDOGE chain-fusion minter canisters expose a `get_minter_info()` query endpoint that returns operational parameters (`deposit_btc_min_amount`, `retrieve_btc_min_amount`, `min_confirmations`, `check_fee`) but **omits the current `Mode`**. When the minter is in `ReadOnly`, `RestrictedTo`, or `DepositsRestrictedTo` mode, `get_minter_info()` still returns the same non-zero deposit and withdrawal minimum amounts, implying the minter is fully operational. Any canister or off-chain integration that reads `get_minter_info()` to determine whether to proceed with a deposit or withdrawal will receive misleading information.

---

### Finding Description

The ckBTC minter defines a `Mode` enum controlling which operations are permitted: [1](#0-0) 

The `Mode` is enforced in `update_balance` (deposit) and `retrieve_btc` (withdrawal): [2](#0-1) 

When `Mode::ReadOnly` or `Mode::RestrictedTo` is active, both `update_balance` and `retrieve_btc` return `TemporarilyUnavailable`. However, the `MinterInfo` struct returned by `get_minter_info()` contains no `mode` field: [3](#0-2) 

The `get_minter_info()` implementation reads only fee/confirmation parameters, never the mode: [4](#0-3) 

The Candid interface confirms `MinterInfo` has no mode field: [5](#0-4) 

The identical pattern exists in the ckDOGE minter: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

Any canister or off-chain integration that calls `get_minter_info()` as a pre-flight check before initiating a deposit or withdrawal will receive a response that appears fully operational (valid `deposit_btc_min_amount`, valid `retrieve_btc_min_amount`) even when the minter is in `ReadOnly` or `RestrictedTo` mode and all state-modifying calls will fail. This is the direct IC analog of the ERC4626 `maxDeposit()`/`maxWithdraw()` returning non-zero values while the actual `deposit()`/`withdraw()` reverts.

Concrete consequences:
- An integrating canister that gates its logic on `get_minter_info()` returning a valid `deposit_btc_min_amount` will proceed to call `update_balance()`, waste cycles, and receive `TemporarilyUnavailable` with no prior indication from the info endpoint.
- An integrating canister that checks `retrieve_btc_min_amount` before calling `retrieve_btc()` will similarly be misled.
- The `Mode::DepositsRestrictedTo` case is particularly subtle: `get_minter_info()` returns the same response regardless of whether the caller is on the allow-list, so a non-whitelisted canister cannot distinguish "I am blocked" from "deposits are open." [8](#0-7) 

---

### Likelihood Explanation

The `mode` field is changed by governance upgrade proposals (via `UpgradeArgs`), which is a realistic operational event (e.g., during minter upgrades, emergency pauses, or migration). The ckBTC minter has been upgraded to `RestrictedTo` mode in production tests. Any canister that integrates with the minter and uses `get_minter_info()` as a readiness check — a natural pattern for composable DeFi canisters on the IC — will be affected whenever the mode is non-`GeneralAvailability`. The `get_minter_info()` endpoint is publicly documented and explicitly intended for integration use. [9](#0-8) 

---

### Recommendation

Add the current `mode` to the `MinterInfo` struct and populate it in `get_minter_info()` for both ckBTC and ckDOGE minters:

```rust
pub struct MinterInfo {
    pub min_confirmations: u32,
    pub retrieve_btc_min_amount: u64,
    pub check_fee: u64,
    pub deposit_btc_min_amount: Option<u64>,
    pub mode: Mode,  // add this
}
```

And in `get_minter_info()`:
```rust
fn get_minter_info() -> MinterInfo {
    read_state(|s| MinterInfo {
        check_fee: s.check_fee,
        min_confirmations: s.min_confirmations,
        retrieve_btc_min_amount: s.fee_based_retrieve_btc_min_amount,
        deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
        mode: s.mode.clone(),  // add this
    })
}
```

This allows callers to determine whether deposits and/or withdrawals are currently available before attempting them, matching the actual behavior of `update_balance()` and `retrieve_btc()`.

---

### Proof of Concept

1. Governance upgrades the ckBTC minter with `Mode::ReadOnly` via `UpgradeArgs { mode: Some(Mode::ReadOnly), .. }`.
2. An integrating canister calls `get_minter_info()` — it receives `{ min_confirmations: 6, retrieve_btc_min_amount: 100000, kyt_fee: 2000, deposit_btc_min_amount: Some(2001) }` with no indication of restricted mode.
3. The integrating canister, seeing a valid `deposit_btc_min_amount`, proceeds to call `update_balance()`.
4. `update_balance()` checks `s.mode.is_deposit_available_for(&caller)`, returns `Err("the minter is in read-only mode")`, and the call fails with `UpdateBalanceError::TemporarilyUnavailable`.
5. The integrating canister has no way to distinguish this from a transient error without re-querying a separate endpoint or parsing the error string. [2](#0-1) [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L339-353)
```rust
/// Controls which operations the minter can perform.
#[derive(
    Default, Clone, Eq, PartialEq, Debug, Serialize, candid::CandidType, serde::Deserialize,
)]
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
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L355-388)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L159-160)
```rust
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L83-91)
```rust
#[derive(Debug, CandidType, Deserialize, Serialize)]
pub struct MinterInfo {
    pub min_confirmations: u32,
    pub retrieve_btc_min_amount: u64,
    // Serialize to the old name to be backward compatible in Candid.
    #[serde(rename = "kyt_fee")]
    pub check_fee: u64,
    pub deposit_btc_min_amount: Option<u64>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L251-259)
```rust
#[query]
fn get_minter_info() -> MinterInfo {
    read_state(|s| MinterInfo {
        check_fee: s.check_fee,
        min_confirmations: s.min_confirmations,
        retrieve_btc_min_amount: s.fee_based_retrieve_btc_min_amount,
        deposit_btc_min_amount: Some(s.effective_deposit_min_btc_amount()),
    })
}
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L376-386)
```text
type MinterInfo = record {
    min_confirmations : nat32;
    // This amount is based on the `retrieve_btc_min_amount` setting during canister
    // initialization or upgrades, but may vary according to current network fees.
    retrieve_btc_min_amount : nat64;
    // The same as `check_fee`, but the old name is kept here to be backward compatible.
    kyt_fee : nat64;
    // Minimal amount of BTC that can be deposited to be converted into ckBTC.
    // UTXOs with lower values will be ignored.
    deposit_btc_min_amount : opt nat64;
};
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L207-214)
```rust
#[query]
fn get_minter_info() -> MinterInfo {
    ic_ckbtc_minter::state::read_state(|s| MinterInfo {
        min_confirmations: s.min_confirmations,
        deposit_doge_min_amount: s.effective_deposit_min_btc_amount(),
        retrieve_doge_min_amount: s.fee_based_retrieve_btc_min_amount,
    })
}
```

**File:** rs/dogecoin/ckdoge/minter/ckdoge_minter.did (L267-279)
```text
type MinterInfo = record {
    // The minimum number of confirmations required for the minter to
    // accept a Dogecoin transaction.
    min_confirmations : nat32;

    // Minimal amount of DOGE that can be deposited to be converted into ckDOGE.
    // UTXOs with lower values will be ignored.
    deposit_doge_min_amount : nat64;

    // This amount is based on the `retrieve_doge_min_amount` setting during canister
    // initialization or upgrades, but may vary according to current network fees.
    retrieve_doge_min_amount : nat64;
};
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L470-512)
```rust
#[test]
fn test_upgrade_restricted() {
    let env = new_state_machine();
    let ledger_id = install_ledger(&env);
    let minter_id = install_minter(&env, ledger_id);

    let authorized_principal =
        Principal::from_str("k2t6j-2nvnp-4zjm3-25dtz-6xhaa-c7boj-5gayf-oj3xs-i43lp-teztq-6ae")
            .unwrap();

    let unauthorized_principal =
        Principal::from_str("gjfkw-yiolw-ncij7-yzhg2-gq6ec-xi6jy-feyni-g26f4-x7afk-thx6z-6ae")
            .unwrap();

    // upgrade
    let upgrade_args = UpgradeArgs {
        mode: Some(Mode::RestrictedTo(vec![authorized_principal])),
        ..Default::default()
    };
    let minter_arg = MinterArg::Upgrade(Some(upgrade_args));
    env.upgrade_canister(minter_id, minter_wasm(), Encode!(&minter_arg).unwrap())
        .expect("Failed to upgrade the minter canister");

    // Check that the unauthorized user cannot modify the state.

    // 1. update_balance
    let update_balance_args = UpdateBalanceArgs {
        owner: None,
        subaccount: None,
    };
    let res = env
        .execute_ingress_as(
            unauthorized_principal.into(),
            minter_id,
            "update_balance",
            Encode!(&update_balance_args).unwrap(),
        )
        .expect("Failed to call update_balance");
    let res = Decode!(&res.bytes(), Result<Vec<UtxoStatus>, UpdateBalanceError>).unwrap();
    assert!(
        matches!(res, Err(UpdateBalanceError::TemporarilyUnavailable(_))),
        "unexpected result: {res:?}"
    );
```
