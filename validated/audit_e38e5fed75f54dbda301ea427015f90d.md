### Title
Missing Per-Token Minimum Amount Validation in `withdraw_erc20` Causes Irreversible ckETH Loss - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `withdraw_erc20` endpoint in the ckETH minter canister lacks a minimum amount check for the ERC20 withdrawal amount before burning ckETH for gas fees. Unlike `withdraw_eth`, which validates the amount against a configurable `cketh_minimum_withdrawal_amount` before any state change, `withdraw_erc20` burns ckETH first and only discovers the ERC20 amount is too small (below the ckERC20 ledger transfer fee) after the ckETH burn is already committed. The user is then reimbursed ckETH minus a `CKETH_LEDGER_TRANSACTION_FEE` penalty — a real, irreversible loss. This is directly analogous to the reported vulnerability: the absence of a decimal-aware per-token minimum allows users of lower-decimal ERC20 tokens (e.g., ckUSDC with 6 decimals) to submit amounts that are trivially below the ledger fee, triggering the penalty path.

---

### Finding Description

`withdraw_eth` performs an explicit upfront minimum check:

```rust
// rs/ethereum/cketh/minter/src/main.rs, lines 291-296
let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
if amount < minimum_withdrawal_amount {
    return Err(WithdrawalError::AmountTooLow {
        min_withdrawal_amount: minimum_withdrawal_amount.into(),
    });
}
```

`withdraw_erc20` performs no equivalent check on the ERC20 `amount`. It immediately proceeds to burn ckETH for the estimated gas fee:

```rust
// rs/ethereum/cketh/minter/src/main.rs, lines 448-458
match cketh_ledger
    .burn_from(cketh_account, erc20_tx_fee, BurnMemo::Erc20GasFee { ... })
    .await
```

Only after the ckETH burn succeeds does it attempt to burn the ERC20 amount. If that amount is below the ckERC20 ledger transfer fee, the ledger returns `AmountTooLow`. The minter then computes the reimbursement as:

```rust
// rs/ethereum/cketh/minter/src/main.rs, lines 507-513
LedgerBurnError::AmountTooLow { .. } => erc20_tx_fee
    .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
    .unwrap_or(Wei::ZERO),
```

The user permanently loses `CKETH_LEDGER_TRANSACTION_FEE` worth of ckETH. The `MinterInfo` struct exposes only a single `minimum_withdrawal_amount` for ckETH; there is no per-token minimum exposed or enforced for any ckERC20 token.

The decimal relevance: ckUSDC uses 6 decimals, ckWBTC uses 8 decimals, ckDAI uses 18 decimals. The ckERC20 ledger transfer fee is set per-token in its own smallest unit. A user of ckUSDC (6 decimals) submitting `amount = 1` (= 0.000001 USDC) will trivially fall below the transfer fee and trigger the penalty path, whereas the same integer `1` in ckDAI (18 decimals) is negligible. There is no upfront guard that is calibrated to the token's decimal scale.

---

### Impact Explanation

Any unprivileged user calling `withdraw_erc20` with an ERC20 amount below the ckERC20 ledger transfer fee will:
1. Have ckETH burned for gas fees (committed, irreversible).
2. Have the ckERC20 burn rejected with `AmountTooLow`.
3. Receive a reimbursement of `erc20_tx_fee − CKETH_LEDGER_TRANSACTION_FEE`, permanently losing `CKETH_LEDGER_TRANSACTION_FEE` worth of ckETH.

The loss is bounded per call but is real and repeatable. Users of lower-decimal tokens (ckUSDC, ckUSDT) are more likely to hit this path accidentally because small integer amounts in those tokens are economically meaningful yet still below the ledger fee.

**Impact: Medium** — direct, irreversible loss of ckETH for any user who submits a sub-minimum ERC20 withdrawal.

---

### Likelihood Explanation

The `withdraw_erc20` endpoint is publicly callable by any non-anonymous principal. The `MinterInfo` query does not expose a per-token minimum, so users have no on-chain way to query the correct minimum before calling. Users of 6-decimal tokens (ckUSDC, ckUSDT) are particularly likely to submit amounts that are below the ledger fee because small integer values in those tokens are common and economically meaningful. The test `should_error_when_ckerc20_withdrawal_amount_too_small` in `rs/ethereum/cketh/minter/tests/ckerc20.rs` (lines 497–530) confirms this path is reachable and results in the penalty.

**Likelihood: Medium** — the endpoint is open to all users, no privileged access is required, and the absence of an upfront minimum makes accidental triggering straightforward for lower-decimal tokens.

---

### Recommendation

1. Add a per-token minimum withdrawal amount to the minter state (analogous to `cketh_minimum_withdrawal_amount`), keyed by `ckerc20_ledger_id`, set to at least the ckERC20 ledger transfer fee for each token.
2. In `withdraw_erc20`, check `ckerc20_withdrawal_amount >= per_token_minimum` before burning any ckETH, returning an `AmountTooLow`-style error immediately if the check fails.
3. Expose per-token minimums in `MinterInfo` so callers can query the correct minimum before submitting.

---

### Proof of Concept

1. Deploy or use the existing ckETH minter with ckUSDC support (6 decimals, transfer fee = e.g. `2_000` USDC-cents).
2. Call `withdraw_erc20` with `amount = 1` (below the ckUSDC ledger transfer fee), a valid `ckerc20_ledger_id` for ckUSDC, and a valid Ethereum recipient.
3. Approve the minter for sufficient ckETH to cover the gas fee estimate.
4. Observe: ckETH is burned for gas, ckUSDC burn fails with `AmountTooLow`, user receives `CkErc20LedgerError { error: AmountTooLow { ... } }` and is reimbursed ckETH minus `CKETH_LEDGER_TRANSACTION_FEE`.

Confirmed by the existing test at: [1](#0-0) 

The asymmetry between `withdraw_eth` (upfront minimum check) and `withdraw_erc20` (no minimum check) is visible at: [2](#0-1) [3](#0-2) 

The penalty reimbursement logic that causes the ckETH loss: [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L497-529)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L289-296)
```rust
    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-432)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
    validate_ckerc20_active();
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");

    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-514)
```rust
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
```
