### Title
Missing Pre-Validation of `amount` in `withdraw_erc20` Causes Unnecessary ckETH Penalty Loss - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `withdraw_erc20` endpoint on the ckETH minter canister does not validate the caller-supplied `amount` against a minimum threshold before initiating the two-step burn sequence (ckETH gas fee burn, then ckERC20 token burn). When a user passes an `amount` below the ckERC20 ledger's `min_burn_amount` (i.e., the transfer fee), the minter burns ckETH for gas fees first, then the ckERC20 burn fails with `AmountTooLow`. The reimbursement is penalized by `CKETH_LEDGER_TRANSACTION_FEE`, causing the user to lose ckETH unnecessarily. The sibling `withdraw_eth` endpoint performs this check upfront and avoids any token loss.

---

### Finding Description

In `withdraw_erc20`, the caller-supplied `amount` is converted to `ckerc20_withdrawal_amount` without any minimum-amount guard: [1](#0-0) 

The function then immediately proceeds to burn ckETH for the estimated gas fee: [2](#0-1) 

Only after the ckETH burn succeeds does the minter attempt to burn the ckERC20 tokens. If `amount` is below the ckERC20 ledger's transfer fee, this second burn fails with `LedgerBurnError::AmountTooLow`. The error handler then applies a penalty: [3](#0-2) 

The user is reimbursed `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`, permanently losing `CKETH_LEDGER_TRANSACTION_FEE` worth of ckETH for a request that could have been rejected before any burn occurred.

By contrast, `withdraw_eth` performs an explicit upfront check before any burn: [4](#0-3) 

This asymmetry is confirmed by the integration test `should_error_when_ckerc20_withdrawal_amount_too_small`, which demonstrates that when `amount = CKERC20_TRANSFER_FEE - 1`, the ckETH burn still executes before the failure is detected: [5](#0-4) 

The `WithdrawErc20Arg.amount` field accepts any `nat` with no lower bound enforced at the endpoint level: [6](#0-5) 

The documentation explicitly states: "Overcharged transaction fees are not reimbursed": [7](#0-6) 

---

### Impact Explanation

Any unprivileged IC principal calling `withdraw_erc20` with `amount` below the ckERC20 ledger's transfer fee will:
1. Have their ckETH burned for the estimated gas fee (irreversible on-ledger burn)
2. Receive a reimbursement reduced by `CKETH_LEDGER_TRANSACTION_FEE`
3. Permanently lose `CKETH_LEDGER_TRANSACTION_FEE` worth of ckETH (~2,000,000,000,000 wei / 0.000002 ckETH per call)

This is a **chain-fusion burn/ledger conservation bug**: tokens are destroyed without the corresponding cross-chain action being completed, and the loss is avoidable with an upfront check. The `Erc20WithdrawalRequest` struct records `withdrawal_amount` as the caller-supplied value with no minimum enforcement: [8](#0-7) 

---

### Likelihood Explanation

**Medium.** The entry path is a direct ingress update call to the publicly exposed `withdraw_erc20` endpoint, requiring only a non-anonymous principal. No privileged access is needed. A user can accidentally trigger this by passing a dust amount (e.g., `amount = 0` or `amount = 1`). The behavior is confirmed by existing integration tests and is reachable on mainnet today. The financial loss per call is small (~0.000002 ckETH) but repeatable and cumulative.

---

### Recommendation

Add an upfront minimum-amount check in `withdraw_erc20` before the ckETH burn, mirroring the pattern in `withdraw_eth`:

```rust
// After computing ckerc20_withdrawal_amount, before any burn:
let min_ckerc20_burn_amount = read_state(|s| {
    s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id)
        .map(|t| t.transfer_fee) // or query the ledger's min_burn_amount
});
if ckerc20_withdrawal_amount < min_ckerc20_burn_amount {
    return Err(WithdrawErc20Error::AmountTooLow { ... });
}
```

Alternatively, add `AmountTooLow` as a new variant to `WithdrawErc20Error` and return it before the ckETH burn is initiated when the amount is provably too small.

---

### Proof of Concept

1. Caller approves the ckETH minter for the gas fee amount on the ckETH ledger.
2. Caller approves the ckETH minter for `1` unit on the ckUSDC ledger.
3. Caller calls `withdraw_erc20` with `amount = 1` (below `CKERC20_TRANSFER_FEE`).
4. Minter burns ckETH for gas fee — **succeeds**.
5. Minter attempts to burn `1` ckUSDC — **fails** with `AmountTooLow`.
6. Minter reimburses `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE` to caller.
7. Caller has permanently lost `CKETH_LEDGER_TRANSACTION_FEE` ckETH.

This is directly demonstrated by the existing test at: [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L415-416)
```rust
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L507-513)
```rust
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L497-530)
```rust
    #[test]
    fn should_error_when_ckerc20_withdrawal_amount_too_small() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
        let caller = ckerc20.caller();
        let ckerc20_tx_fee = CKETH_MINIMUM_WITHDRAWAL_AMOUNT;

        ckerc20
            .deposit_cketh_and_ckerc20(
                EXPECTED_BALANCE,
                TWO_USDC + CKERC20_TRANSFER_FEE,
                ckusdc.clone(),
                caller,
            )
            .expect_mint()
            .call_cketh_ledger_approve_minter(caller, ckerc20_tx_fee, None)
            .call_ckerc20_ledger_approve_minter(ckusdc.ledger_canister_id, caller, TWO_USDC, None)
            .call_minter_withdraw_erc20(
                caller,
                CKERC20_TRANSFER_FEE - 1,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(WithdrawErc20Error::CkErc20LedgerError {
                cketh_block_index: 2_u8.into(),
                error: LedgerError::AmountTooLow {
                    minimum_burn_amount: CKERC20_TRANSFER_FEE.into(),
                    failed_burn_amount: Nat::from(CKERC20_TRANSFER_FEE - 1),
                    token_symbol: "ckUSDC".to_string(),
                    ledger_id: ckusdc.ledger_canister_id,
                },
            });
    }
```

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L6-13)
```rust
#[derive(CandidType, Deserialize)]
pub struct WithdrawErc20Arg {
    pub amount: Nat,
    pub ckerc20_ledger_id: Principal,
    pub recipient: String,
    pub from_cketh_subaccount: Option<Subaccount>,
    pub from_ckerc20_subaccount: Option<Subaccount>,
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-177)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
    #[n(1)]
    pub withdrawal_amount: Erc20Value,
    /// The recipient's address of the sent ERC-20 tokens.
    #[n(2)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation on the ckETH ledger.
    #[cbor(n(3), with = "crate::cbor::id")]
    pub cketh_ledger_burn_index: LedgerBurnIndex,
    /// Address of the ERC-20 smart contract that is the message call's recipient.
    #[n(4)]
    pub erc20_contract_address: Address,
    /// The ckERC20 ledger on which the minter burned the ckERC20 tokens.
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
    /// The transaction ID of the ckERC20 burn operation on the ckERC20 ledger.
    #[cbor(n(6), with = "crate::cbor::id")]
    pub ckerc20_ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(9)]
    pub created_at: u64,
}
```
