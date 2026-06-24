### Title
`estimate_withdrawal_fee` Returns Non-Zero Fee When Withdrawals Are Disabled by Minter Mode - (`rs/bitcoin/ckbtc/minter/src/main.rs`, `rs/dogecoin/ckdoge/minter/src/main.rs`)

---

### Summary

The ckBTC and ckDOGE minters expose an `estimate_withdrawal_fee` query endpoint that returns a valid, non-zero fee estimate regardless of whether the minter's operational `Mode` actually permits withdrawals. When the minter is in `ReadOnly` or `RestrictedTo` mode (blocking withdrawals for the caller), the fee estimation endpoint gives no indication that withdrawals are unavailable. This is the direct IC analog of the ERC-4626 `maxWithdraw` not returning 0 when withdrawals are disabled.

---

### Finding Description

The ckBTC minter defines a `Mode` enum with four variants that control which operations are permitted: [1](#0-0) 

The `is_withdrawal_available_for` method returns an error for `ReadOnly` and `RestrictedTo` (for non-allowlisted principals): [2](#0-1) 

Both `retrieve_btc` and `retrieve_btc_with_approval` correctly enforce this check before proceeding: [3](#0-2) [4](#0-3) 

However, the `estimate_withdrawal_fee` query endpoint performs **no mode check whatsoever**. It unconditionally computes and returns a fee estimate: [5](#0-4) 

The same omission exists in the ckDOGE minter's `estimate_withdrawal_fee`: [6](#0-5) 

The `WithdrawalFee` struct returned contains `minter_fee` and `bitcoin_fee` fields with positive values, giving no indication that the withdrawal operation would be rejected: [7](#0-6) 

---

### Impact Explanation

**Funds temporarily locked in withdrawal account (old `retrieve_btc` flow):** The legacy `retrieve_btc` endpoint requires users to first transfer ckBTC to the minter's withdrawal sub-account (via `get_withdrawal_account`), then call `retrieve_btc`. An integrator or user who:

1. Calls `estimate_withdrawal_fee` → receives a valid fee estimate (no indication of `ReadOnly`/`RestrictedTo` mode)
2. Transfers ckBTC to the withdrawal account
3. Calls `retrieve_btc` → receives `TemporarilyUnavailable`

...will have their ckBTC locked in the withdrawal sub-account until the minter mode changes. The user cannot reclaim these funds without the minter exiting restricted mode and successfully completing the withdrawal. This is a temporary but real lock-up of user funds caused by a misleading API.

**Misleading integrator behavior (approval-based flow):** For `retrieve_btc_with_approval`, the impact is that integrators building automated systems (e.g., wallets, DeFi protocols) that use `estimate_withdrawal_fee` as a proxy for "is withdrawal available?" will be misled into attempting withdrawals that will fail, wasting ICRC-2 approvals and causing user-facing errors.

The `ReadOnly` mode is a documented operational state used during minter upgrades and emergency situations, making this a realistic scenario.

---

### Likelihood Explanation

The `ReadOnly` and `RestrictedTo` modes are explicitly supported operational states used during minter upgrades and emergency interventions. The ckBTC minter is a high-value production canister. Any integrator or user who queries `estimate_withdrawal_fee` during such a period (which is not signaled by the endpoint) and then proceeds with the old `retrieve_btc` flow will have funds temporarily locked. The entry path requires only an unprivileged ingress call to the query endpoint, accessible to any IC user.

---

### Recommendation

Add a mode check at the start of `estimate_withdrawal_fee` in both the ckBTC minter (`rs/bitcoin/ckbtc/minter/src/main.rs`) and the ckDOGE minter (`rs/dogecoin/ckdoge/minter/src/main.rs`). When `is_withdrawal_available_for` returns an error for the caller, the endpoint should either trap with a descriptive message or return a zero-valued `WithdrawalFee` (analogous to ERC-4626's `maxWithdraw` returning 0). The mode check should use `ic_cdk::api::msg_caller()` consistent with how the actual withdrawal endpoints enforce it.

---

### Proof of Concept

1. Deploy ckBTC minter and upgrade it to `Mode::ReadOnly`:
   ```
   // As seen in test_upgrade_read_only (rs/bitcoin/ckbtc/minter/tests/tests.rs:410-468)
   upgrade_args = UpgradeArgs { mode: Some(Mode::ReadOnly), .. }
   ```

2. Call `estimate_withdrawal_fee` as any principal:
   ```
   dfx canister call minter estimate_withdrawal_fee '(record { amount = opt 50_000_000 })'
   // Returns: record { bitcoin_fee = <N>; minter_fee = <M> }  ← no error, positive values
   ```

3. Transfer ckBTC to the withdrawal account returned by `get_withdrawal_account`.

4. Call `retrieve_btc` with the same amount:
   ```
   dfx canister call minter retrieve_btc '(record { address = "bc1q..."; amount = 50_000_000 })'
   // Returns: Err(TemporarilyUnavailable("the minter is in read-only mode"))
   ```

5. The ckBTC transferred in step 3 is now locked in the withdrawal sub-account. The `estimate_withdrawal_fee` endpoint gave no indication that step 4 would fail. [5](#0-4) [8](#0-7) [9](#0-8) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L339-388)
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L248-251)
```rust
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L219-249)
```rust
#[query]
fn estimate_withdrawal_fee(arg: EstimateFeeArg) -> WithdrawalFee {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    match mutate_state(|s| {
        let fee_estimator = IC_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);
        ic_ckbtc_minter::estimate_retrieve_btc_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
    }) {
        Ok(fee) => fee,
        Err(BuildTxError::NotEnoughFunds) => {
            panic!("ERROR: withdrawal amount is too large for the minter")
        }
        Err(e @ BuildTxError::DustOutput { .. } | e @ BuildTxError::AmountTooLow) => panic!(
            "BUG: withdrawal amount is too low ({e:?}), but the withdrawal amount should be large enough to prevent this"
        ),
        Err(BuildTxError::InvalidTransaction(
            e @ InvalidTransactionError::TooManyInputs { .. },
        )) => panic!(
            "ERROR: the minter cannot currently process such a large withdrawal amount because it would require too many inputs ({e:?}), \
            resulting in the transaction being potentially non-standard"
        ),
    }
}
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L98-128)
```rust
#[query]
fn estimate_withdrawal_fee(
    arg: EstimateFeeArg,
) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    ic_ckbtc_minter::state::mutate_state(|s| {
        let fee_estimator = DOGECOIN_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);

        ic_ckdoge_minter::fees::estimate_retrieve_doge_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
        .map_err(|e| match e {
            BuildTxError::NotEnoughFunds
            | BuildTxError::InvalidTransaction(InvalidTransactionError::TooManyInputs { .. }) => {
                EstimateWithdrawalFeeError::AmountTooHigh
            }
            BuildTxError::AmountTooLow | BuildTxError::DustOutput { .. } => {
                EstimateWithdrawalFeeError::AmountTooLow {
                    min_amount: s.fee_based_retrieve_btc_min_amount,
                }
            }
        })
    })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L31-35)
```rust
#[derive(Copy, Clone, Eq, PartialEq, Debug, CandidType, Serialize, Deserialize, Default)]
pub struct WithdrawalFee {
    pub minter_fee: u64,
    pub bitcoin_fee: u64,
}
```
