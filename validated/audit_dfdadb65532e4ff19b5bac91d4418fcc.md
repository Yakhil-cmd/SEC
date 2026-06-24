Audit Report

## Title
Missing Minimum ERC20 Amount Validation in `withdraw_erc20` Burns ckETH Before Rejecting Dust Withdrawals - (File: `rs/ethereum/cketh/minter/src/main.rs`)

## Summary

The `withdraw_erc20` endpoint burns ckETH to cover the ERC20 gas fee before validating that the requested ckERC20 withdrawal amount meets the ledger's minimum burn threshold. Any unprivileged caller can submit a dust ckERC20 withdrawal (e.g., `amount = 1`), causing the minter to irreversibly burn ckETH, emit a `FailedErc20WithdrawalRequest` event, and enqueue a reimbursement task — all without any pre-flight amount check. Repeated sequential calls from one or more principals grow the minter's reimbursement queue and event log unboundedly.

## Finding Description

In `withdraw_erc20` (lines 389–543), the caller-supplied `amount` is converted directly to `Erc20Value` with no minimum check: [1](#0-0) 

The function then estimates the gas fee and **burns ckETH** at line 448–458 before attempting the ckERC20 burn: [2](#0-1) 

Only after the ckETH burn succeeds does it attempt to burn the ckERC20 amount. If `ckerc20_withdrawal_amount` is below the ledger's transfer fee, the ckERC20 burn fails with `LedgerBurnError::AmountTooLow`. At that point, the minter emits a `FailedErc20WithdrawalRequest` event and enqueues a reimbursement (returning `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`): [3](#0-2) 

By contrast, `withdraw_eth` validates the ETH amount against `cketh_minimum_withdrawal_amount` **before** any ledger interaction: [4](#0-3) 

`WithdrawErc20Error` has no `AmountTooLow` variant for the ERC20 amount — the only rejection path for a dust ERC20 amount is the downstream ledger call, reached only after ckETH is already burned: [5](#0-4) 

The existing test `should_error_when_ckerc20_withdrawal_amount_too_small` confirms this burn-then-fail flow as expected behavior. The `cketh_block_index: 2_u8.into()` in the expected error proves ckETH was already burned before the ERC20 amount was validated: [6](#0-5) 

## Impact Explanation

Each dust `withdraw_erc20` call with `amount < ckerc20_ledger_transfer_fee`:
- Burns ckETH from the caller (gas fee estimate, typically `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE`)
- Emits a `FailedErc20WithdrawalRequest` event, permanently stored in the minter's event log
- Enqueues a `ReimbursementRequest` in the minter's reimbursement queue, which must be processed by the minter's timer

Repeated calls grow the reimbursement queue and event log unboundedly. The minter's timer-driven reimbursement loop must process each entry individually, consuming cycles and delaying legitimate withdrawal processing. The minter's stable state (event log + reimbursement map) grows proportionally to the number of spam calls, increasing upgrade and state-replay costs. This constitutes a significant Chain Fusion / ck-token infrastructure security impact with concrete user and protocol harm, qualifying as **High ($2,000–$10,000)** under the "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm" category.

## Likelihood Explanation

The `withdraw_erc20` endpoint is publicly callable by any non-anonymous principal on mainnet. The attacker's cost per call is `CKETH_LEDGER_TRANSACTION_FEE` (the penalty deducted from the reimbursement), a small fixed amount. No privileged access, governance majority, or threshold corruption is required. The per-principal guard `retrieve_withdraw_guard` prevents concurrent calls from the same principal but not sequential calls or calls from different principals. Any holder of a small amount of ckETH can execute this repeatedly at low cost.

## Recommendation

Add a minimum ERC20 amount check **before** burning ckETH, analogous to the check in `withdraw_eth`. The minimum should be at least the ckERC20 ledger's transfer fee. A new `WithdrawErc20Error::AmountTooLow` variant should be added to `rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs` to surface this rejection cleanly:

```rust
// After resolving ckerc20_token, before burning ckETH:
let min_ckerc20_amount = /* ckerc20 ledger transfer fee, read from state or a constant */;
if ckerc20_withdrawal_amount < min_ckerc20_amount {
    return Err(WithdrawErc20Error::AmountTooLow {
        token_symbol: ckerc20_token.ckerc20_token_symbol,
        minimum_withdrawal_amount: min_ckerc20_amount.into(),
    });
}
```

This mirrors the pattern already used in `withdraw_eth` at lines 291–296.

## Proof of Concept

1. Obtain a small amount of ckETH (enough to cover `erc20_tx_fee` for several calls) and approve the minter.
2. Obtain 1 unit of a supported ckERC20 token (e.g., 1 ckUSDC-cent, below the ledger transfer fee of 10,000 ckUSDC-cents) and approve the minter.
3. Call `withdraw_erc20` with `amount = CKERC20_TRANSFER_FEE - 1`.
4. Observe: ckETH gas fee is burned, `FailedErc20WithdrawalRequest` event is emitted, reimbursement is enqueued.
5. Repeat step 3 sequentially (the guard releases after each call completes).
6. Observe: the minter's reimbursement queue and event log grow with each call.

The existing state machine test `should_error_when_ckerc20_withdrawal_amount_too_small` at `rs/ethereum/cketh/minter/tests/ckerc20.rs` lines 497–530 reproduces this flow exactly and confirms no pre-flight amount guard exists.

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-535)
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
                    if reimbursed_amount > Wei::ZERO {
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::FailedErc20WithdrawalRequest(reimbursement_request),
                            );
                        });
                    }
                    Err(WithdrawErc20Error::CkErc20LedgerError {
                        cketh_block_index: Nat::from(cketh_ledger_burn_index.get()),
                        error: ckerc20_burn_error.into(),
                    })
```

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L30-46)
```rust
#[derive(Clone, PartialEq, Debug, CandidType, Deserialize)]
pub enum WithdrawErc20Error {
    TokenNotSupported {
        supported_tokens: Vec<crate::endpoints::CkErc20Token>,
    },
    RecipientAddressBlocked {
        address: String,
    },
    CkEthLedgerError {
        error: LedgerError,
    },
    CkErc20LedgerError {
        cketh_block_index: Nat,
        error: LedgerError,
    },
    TemporarilyUnavailable(String),
}
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
