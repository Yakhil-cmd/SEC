### Title
Missing Minimum ERC20 Amount Validation in `withdraw_erc20` Burns ckETH Before Rejecting Dust Withdrawals - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `withdraw_erc20` endpoint in the ckETH minter canister burns ckETH gas fees **before** validating that the requested ckERC20 withdrawal amount meets the ledger's minimum burn threshold. Any unprivileged caller can submit a dust ckERC20 withdrawal (e.g., `amount = 1`), causing the minter to irreversibly burn ckETH from the caller's account, emit a `FailedErc20WithdrawalRequest` event, and enqueue a reimbursement task — all without any pre-flight amount check. Repeated calls grow the minter's reimbursement queue and event log, forcing the minter to process spurious reimbursement work.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/main.rs`, the `withdraw_erc20` function accepts any `amount : nat` from the caller without validating it against a minimum threshold before initiating ledger operations: [1](#0-0) 

The function converts the caller-supplied amount directly to `Erc20Value` and proceeds to:

1. Estimate the Ethereum gas fee (`estimate_erc20_transaction_fee`)
2. **Burn ckETH** from the caller's account to cover that gas fee
3. Only then attempt to burn the ckERC20 amount from the caller's account [2](#0-1) 

If `ckerc20_withdrawal_amount` is below the ckERC20 ledger's transfer fee, the second burn fails with `LedgerBurnError::AmountTooLow`. At this point the ckETH gas fee is already consumed. The minter then emits a `FailedErc20WithdrawalRequest` event and enqueues a reimbursement task (returning `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE` to the caller): [3](#0-2) 

The `WithdrawErc20Error` type has no `AmountTooLow` variant for the ERC20 amount itself — the only rejection path for a dust ERC20 amount is the downstream ledger call, which is reached only after ckETH is already burned: [4](#0-3) 

By contrast, `withdraw_eth` correctly validates the ETH amount against `cketh_minimum_withdrawal_amount` **before** any ledger interaction: [5](#0-4) 

The ckBTC and ckDOGE minters similarly enforce minimum deposit amounts before processing: [6](#0-5) [7](#0-6) 

The behavior is confirmed by an existing test that documents the ckETH burn-then-fail flow as expected behavior rather than a guarded path: [8](#0-7) 

---

### Impact Explanation

Each dust `withdraw_erc20` call with `amount < ckerc20_ledger_transfer_fee`:

- Burns ckETH from the caller (gas fee estimate, typically on the order of the `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE`)
- Emits a `FailedErc20WithdrawalRequest` event, permanently stored in the minter's event log
- Enqueues a `ReimbursementRequest` in the minter's reimbursement queue, which must be processed by the minter's timer

Repeated calls from multiple principals (each paying `CKETH_LEDGER_TRANSACTION_FEE` as a penalty) grow the reimbursement queue and event log unboundedly. The minter's timer-driven reimbursement loop must process each entry individually, consuming cycles and delaying legitimate withdrawal processing. The minter's stable state (event log + reimbursement map) grows proportionally to the number of spam calls, increasing upgrade and state-replay costs. [9](#0-8) 

---

### Likelihood Explanation

The `withdraw_erc20` endpoint is publicly callable by any non-anonymous principal on mainnet. The attacker's cost per call is `CKETH_LEDGER_TRANSACTION_FEE` (the penalty deducted from the reimbursement), which is a small fixed amount. No privileged access, governance majority, or threshold corruption is required. The attack is sequential (the per-principal guard `retrieve_withdraw_guard` prevents concurrent calls from the same principal, but not sequential calls or calls from different principals). Any holder of a small amount of ckETH can execute this repeatedly. [10](#0-9) 

---

### Recommendation

Add a minimum ERC20 amount check **before** burning ckETH, analogous to the check in `withdraw_eth`. The minimum should be at least the ckERC20 ledger's transfer fee. A new `WithdrawErc20Error::AmountTooLow` variant should be added to surface this rejection cleanly:

```rust
// After resolving ckerc20_token, before burning ckETH:
let min_ckerc20_amount = /* ckerc20 ledger transfer fee, e.g. read from state or a constant */;
if ckerc20_withdrawal_amount < min_ckerc20_amount {
    return Err(WithdrawErc20Error::AmountTooLow {
        token_symbol: ckerc20_token.ckerc20_token_symbol,
        minimum_withdrawal_amount: min_ckerc20_amount.into(),
    });
}
```

This mirrors the pattern already used in `withdraw_eth`: [5](#0-4) 

---

### Proof of Concept

1. Obtain a small amount of ckETH (enough to cover `erc20_tx_fee` for several calls) and approve the minter.
2. Obtain 1 unit of a supported ckERC20 token (e.g., 1 ckUSDC = below the ledger transfer fee of 10,000 ckUSDC-cents) and approve the minter.
3. Call `withdraw_erc20` with `amount = 1` (below the ckERC20 ledger transfer fee).
4. Observe: ckETH gas fee is burned, `FailedErc20WithdrawalRequest` event is emitted, reimbursement is enqueued.
5. Repeat step 3 sequentially (the guard releases after each call completes).
6. Observe: the minter's reimbursement queue and event log grow with each call; the minter's timer must process each reimbursement individually.

The existing state machine test confirms this flow executes without any pre-flight amount guard: [8](#0-7)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-416)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-478)
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
    {
        Ok(cketh_ledger_burn_index) => {
            log!(
                INFO,
                "[withdraw_erc20]: burning {} {} from account {}",
                ckerc20_withdrawal_amount,
                ckerc20_token.ckerc20_token_symbol,
                ckerc20_account
            );
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
            {
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L276-300)
```rust
    for utxo in processable_utxos {
        let ignored_reason = if utxo.value < deposit_btc_min_amount {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is lower than the minimum deposit amount {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(deposit_btc_min_amount)
            ))
        } else if utxo.value <= check_fee {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is not higher than the check fee {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(check_fee)
            ))
        } else {
            None
        };
        if let Some(ignored_reason) = ignored_reason {
            mutate_state(|s| {
                state::audit::ignore_utxo(s, utxo.clone(), caller_account, now, runtime)
            });
            log!(Priority::Debug, "{ignored_reason}");
            utxo_statuses.push(UtxoStatus::ValueTooSmall(utxo));
            continue;
```

**File:** rs/dogecoin/ckdoge/minter/ckdoge_minter.did (L272-274)
```text
    // Minimal amount of DOGE that can be deposited to be converted into ckDOGE.
    // UTXOs with lower values will be ignored.
    deposit_doge_min_amount : nat64;
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

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L16-37)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Decode, Encode)]
pub enum EventType {
    /// The minter initialization event.
    /// Must be the first event in the log.
    #[n(0)]
    Init(#[n(0)] InitArg),
    /// The minter upgraded with the specified arguments.
    #[n(1)]
    Upgrade(#[n(0)] UpgradeArg),
    /// The minter discovered a ckETH deposit in the helper contract logs.
    #[n(2)]
    AcceptedDeposit(#[n(0)] ReceivedEthEvent),
    /// The minter discovered an invalid ckETH deposit in the helper contract logs.
    #[n(4)]
    InvalidDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The reason why minter considers the deposit invalid.
        #[n(1)]
        reason: String,
    },
```
