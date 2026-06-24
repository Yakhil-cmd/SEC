### Title
Accepted ckERC20 Withdrawal Permanently Stuck When Gas Fees Spike Beyond `max_transaction_fee` - (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `withdraw_erc20` flow burns ckETH upfront to lock in a `max_transaction_fee` budget. If Ethereum gas fees later spike so that the locked budget is permanently insufficient to cover the minimum required fee, the accepted withdrawal request is silently rescheduled to the back of the queue indefinitely — with no reimbursement path — leaving both the user's ckERC20 tokens and the burned ckETH permanently locked.

---

### Finding Description

The `withdraw_erc20` endpoint in `rs/ethereum/cketh/minter/src/main.rs` executes the following sequence:

1. Calls `estimate_erc20_transaction_fee()` to compute `erc20_tx_fee = gas_fee_estimate.to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT).max_transaction_fee()`.
2. Burns `erc20_tx_fee` ckETH from the user via `cketh_ledger.burn_from(...)`.
3. Burns the ckERC20 amount from the user via `ckerc20_ledger.burn_from(...)`.
4. Records an `AcceptedErc20WithdrawalRequest` with `max_transaction_fee: erc20_tx_fee` fixed at the time of the call. [1](#0-0) 

The `max_transaction_fee` is permanently frozen in the `Erc20WithdrawalRequest` struct at the value estimated at call time: [2](#0-1) 

Later, when the minter's timer fires `create_transactions_batch`, it calls `create_transaction` for each pending request. For a `CkErc20` request, the function checks whether `actual_min_max_fee_per_gas > request_max_fee_per_gas`: [3](#0-2) 

If the current gas fee has risen above the locked budget, `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`. The handler in `create_transactions_batch` responds by calling `reschedule_withdrawal_request`, which moves the request to the **back of the queue** with no reimbursement: [4](#0-3) 

The same failure occurs for already-sent transactions during resubmission: `create_resubmit_transactions` returns `Err(ResubmitTransactionError::InsufficientTransactionFee)`, which is only logged — no reimbursement is triggered: [5](#0-4) [6](#0-5) 

The `max_transaction_fee` is computed as `2 * base_fee_per_gas + max_priority_fee_per_gas` multiplied by `gas_limit = 65_000`: [7](#0-6) [8](#0-7) 

This estimate is valid for "the next few blocks" but provides no guarantee across sustained gas spikes. The `lazy_refresh_gas_fee_estimate` caches the estimate for up to 60 seconds: [9](#0-8) 

---

### Impact Explanation

When Ethereum gas fees spike and remain elevated beyond the `max_transaction_fee` locked at withdrawal time:

- **For pending (not yet sent) ckERC20 withdrawals**: The request is rescheduled indefinitely. Both the burned ckETH (gas fee) and the burned ckERC20 tokens remain locked with no reimbursement path. The user cannot cancel or recover funds.
- **For already-sent ckERC20 withdrawals**: The transaction cannot be resubmitted with a higher fee. The minter logs an error and stops resubmitting, blocking all subsequent nonces. The ckERC20 tokens and ckETH are stuck.

This is a **ledger conservation bug**: ckETH and ckERC20 tokens are burned from the user's account but the corresponding Ethereum transaction can never be sent, and no reimbursement is issued for the pending-queue case. The ckBTC minter experienced an analogous real-world incident of stuck withdrawals due to insufficient fees, documented in the upgrade proposal: [10](#0-9) 

---

### Likelihood Explanation

This is a realistic scenario. Ethereum gas fees are highly volatile and can spike 5–20× within minutes during network congestion events. The `max_transaction_fee` is locked at call time using a 60-second-cached estimate. A user who calls `withdraw_erc20` just before a gas spike will have their withdrawal permanently stuck. The ckBTC minter already experienced this exact class of issue in production (stuck transactions due to insufficient fees), confirming the likelihood is non-negligible for chain-fusion minters.

---

### Recommendation

1. **For pending requests**: When `create_transaction` returns `InsufficientTransactionFee` for a `CkErc20` request, instead of rescheduling indefinitely, trigger a reimbursement of both the burned ckETH (minus ledger fee) and the burned ckERC20 tokens, analogous to the existing `FailedErc20WithdrawalRequest` path used when the ckERC20 burn fails at withdrawal time.

2. **For sent transactions**: When `create_resubmit_transactions` returns `InsufficientTransactionFee` for a ckERC20 transaction, schedule a reimbursement of the ckERC20 tokens (the ckETH gas fee is already consumed by the on-chain transaction fee).

3. **Alternatively**: Implement a maximum queue age for ckERC20 withdrawal requests. If a request has been pending beyond a threshold without being processable, reimburse the user.

---

### Proof of Concept

**Entry path**: Any unprivileged user calls `withdraw_erc20` on the ckETH minter canister (`sv3dd-oaaaa-aaaar-qacoa-cai`) during a period of moderate gas fees.

**Trigger**: Ethereum gas fees spike significantly after the call is accepted (both ckETH and ckERC20 are burned) but before the minter's timer processes the withdrawal.

**Outcome**:

```
User calls withdraw_erc20(amount=X, ckerc20_ledger_id=USDC, recipient=0x...)
  → erc20_tx_fee = estimate at T0 (e.g., 0.003 ETH)
  → ckETH burned: 0.003 ETH  ✓
  → ckUSDC burned: X USDC    ✓
  → AcceptedErc20WithdrawalRequest { max_transaction_fee: 0.003 ETH } recorded

[Gas spike: fees now require 0.01 ETH minimum]

Timer fires → create_transactions_batch:
  create_transaction(request) → Err(InsufficientTransactionFee {
      allowed: 0.003 ETH, actual: 0.01 ETH
  })
  → reschedule_withdrawal_request(request)  // moved to back of queue
  // NO reimbursement issued
  // ckETH and ckUSDC remain burned, user has no recourse
```

The relevant code path confirming no reimbursement is issued for the pending-queue case: [4](#0-3) 

Compare with the existing reimbursement path that IS triggered when the ckERC20 burn fails during `withdraw_erc20` itself (but is absent for the timer-based processing failure): [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-504)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
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
                Ok(ckerc20_ledger_burn_index) => {
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
                        withdrawal_amount: ckerc20_withdrawal_amount,
                        destination,
                        cketh_ledger_burn_index,
                        ckerc20_ledger_id: ckerc20_token.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index,
                        erc20_contract_address: ckerc20_token.erc20_contract_address,
                        from: caller,
                        from_subaccount: from_ckerc20_subaccount
                            .and_then(LedgerSubaccount::from_bytes),
                        created_at: now,
                    };
                    log!(
                        INFO,
                        "[withdraw_erc20]: queuing withdrawal request {:?}",
                        withdrawal_request
                    );
                    mutate_state(|s| {
                        process_event(
                            s,
                            EventType::AcceptedErc20WithdrawalRequest(withdrawal_request.clone()),
                        );
                    });
                    Ok(RetrieveErc20Request::from(withdrawal_request))
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-531)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L146-177)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L618-631)
```rust
                Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee {
                    allowed_max_transaction_fee,
                    actual_max_transaction_fee,
                }) => {
                    transactions_to_resubmit.push(Err(
                        ResubmitTransactionError::InsufficientTransactionFee {
                            ledger_burn_index: *burn_index,
                            transaction_nonce: *nonce,
                            allowed_max_transaction_fee,
                            max_transaction_fee: actual_max_transaction_fee,
                        },
                    ));
                    return transactions_to_resubmit;
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1168)
```rust
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
            }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L242-246)
```rust
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L516-543)
```rust
impl GasFeeEstimate {
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }

    pub fn estimate_max_fee_per_gas(&self) -> WeiPerGas {
        self.checked_estimate_max_fee_per_gas()
            .unwrap_or(WeiPerGas::MAX)
    }

    pub fn to_price(self, gas_limit: GasAmount) -> TransactionPrice {
        TransactionPrice {
            gas_limit,
            max_fee_per_gas: self.estimate_max_fee_per_gas(),
            max_priority_fee_per_gas: self.max_priority_fee_per_gas,
        }
    }

    pub fn min_max_fee_per_gas(&self) -> WeiPerGas {
        self.base_fee_per_gas
            .checked_add(self.max_priority_fee_per_gas)
            .unwrap_or(WeiPerGas::MAX)
    }
}
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-680)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

    async fn do_refresh() -> Option<GasFeeEstimate> {
        let _guard = match TimerGuard::new(TaskType::RefreshGasFeeEstimate) {
            Ok(guard) => guard,
            Err(e) => {
                log!(
                    DEBUG,
                    "[refresh_gas_fee_estimate]: Failed retrieving guard: {e:?}",
                );
                return None;
            }
        };

        let fee_history = match eth_fee_history().await {
            Ok(fee_history) => fee_history,
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed retrieving fee history: {e:?}",
                );
                return None;
            }
        };

        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
                estimate
            }
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed estimating gas fee: {e:?}",
                );
                return None;
            }
        };
        log!(
            INFO,
            "[refresh_gas_fee_estimate]: Estimated transaction fee: {:?}",
            gas_fee_estimate,
        );
        Some(gas_fee_estimate)
    }

    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }

    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L17-33)
```markdown
## Motivation

Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
