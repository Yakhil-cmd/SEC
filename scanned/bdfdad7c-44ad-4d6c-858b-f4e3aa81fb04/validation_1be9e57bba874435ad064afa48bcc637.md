### Title
`eip_1559_transaction_price` Query Returns Stale Cached Gas Fee Estimate, Causing ckERC20 Withdrawal Failures - (`rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `eip_1559_transaction_price` query endpoint in the ckETH minter returns a stale cached `last_transaction_price_estimate` without refreshing it. Users rely on this estimate to approve the correct amount of ckETH for the minter to burn as gas fees for ckERC20 withdrawals. When gas prices rise between the time the estimate was cached and the time `withdraw_erc20` is processed, the minter's freshly-fetched fee exceeds the user's approval, causing the withdrawal to fail with `CkEthLedgerError { InsufficientAllowance }`.

---

### Finding Description

**Root cause — query endpoint reads stale cached state:**

`eip_1559_transaction_price` is a `#[query]` endpoint. Query calls on the IC cannot perform HTTPS outcalls or inter-canister calls, so the endpoint reads directly from the in-memory cache `s.last_transaction_price_estimate`:

```rust
// rs/ethereum/cketh/minter/src/main.rs:169-198
#[query]
async fn eip_1559_transaction_price(token: Option<Eip1559TransactionPriceArg>) -> Eip1559TransactionPrice {
    ...
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((ts, estimate)) => { ... }
        None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
    }
}
```

The cache is only updated by `lazy_refresh_gas_fee_estimate()`, an async function that makes HTTPS outcalls to Ethereum RPC providers. It is called exclusively during `withdraw_erc20` processing. If no withdrawals have been processed recently, `last_transaction_price_estimate` can be arbitrarily old — there is no background timer that keeps it fresh.

**Divergence at execution time:**

When `withdraw_erc20` is called, it invokes `estimate_erc20_transaction_fee()` → `lazy_refresh_gas_fee_estimate()`, which re-fetches the current fee from Ethereum if the cached value is older than 60 seconds:

```rust
// rs/ethereum/cketh/minter/src/tx.rs:672-680
let now_ns = ic_cdk::api::time();
match read_state(|s| s.last_transaction_price_estimate.clone()) {
    Some((last_estimate_timestamp_ns, estimate))
        if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) => { Some(estimate) }
    _ => do_refresh().await,
}
```

The freshly-fetched fee is then used to burn ckETH from the user's approval:

```rust
// rs/ethereum/cketh/minter/src/main.rs:430-432, 448-458
let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| { ... })?;
...
match cketh_ledger.burn_from(cketh_account, erc20_tx_fee, ...).await {
    Err(cketh_burn_error) => Err(WithdrawErc20Error::CkEthLedgerError { error: cketh_burn_error.into() }),
```

If the refreshed fee exceeds the user's approval (which was based on the stale query result), `burn_from` fails with `InsufficientAllowance`.

**The protocol documentation confirms the intended user flow:**

The official ckERC20 documentation (`rs/ethereum/cketh/docs/ckerc20.adoc`, line 241) states:

> "The user calls the ckETH ledger to approve the minter to burn some of the user's ckETH tokens to pay for the transaction fees. The exact amount of ckETH needed depends on the current Ethereum gas price, which can greatly fluctuate."

Users who call `eip_1559_transaction_price` to get a precise estimate and approve exactly that amount are vulnerable. The stale query result and the live execution-time estimate can diverge significantly during gas price spikes.

---

### Impact Explanation

A user who:
1. Calls `eip_1559_transaction_price` (query) and receives a stale low estimate,
2. Approves the minter for exactly that amount via `icrc2_approve` on the ckETH ledger,
3. Then calls `withdraw_erc20`,

will have their withdrawal rejected with `WithdrawErc20Error::CkEthLedgerError { error: LedgerError::InsufficientAllowance }` whenever gas prices have risen since the cached estimate was stored. The user's ckERC20 tokens remain locked until they re-approve with a higher amount. During periods of high gas price volatility, this can affect many users simultaneously and block the ckERC20 → ERC-20 conversion path entirely for users who followed the documented flow.

---

### Likelihood Explanation

Ethereum gas prices are volatile and can spike significantly within minutes. The `last_transaction_price_estimate` is only refreshed during `withdraw_erc20` processing, and only if the cached value is older than 60 seconds. During low-activity periods (no recent withdrawals), the cache can be hours old. Any user who calls `eip_1559_transaction_price` during such a period and approves a precise amount will be vulnerable to the next gas price increase. This is a realistic, recurring scenario on mainnet.

---

### Recommendation

1. **Add a periodic timer task** that calls `lazy_refresh_gas_fee_estimate()` at least every 60 seconds, ensuring `last_transaction_price_estimate` is never stale when users query it.
2. **Apply a safety multiplier** in `eip_1559_transaction_price`: return `max_transaction_fee * 1.2` (or similar) to account for gas price fluctuations between query time and execution time.
3. **Document clearly** that the returned estimate may be up to 60+ seconds old and that users should add a buffer to their approval amount.

---

### Proof of Concept

1. No `withdraw_erc20` calls have been processed for >60 seconds (low-activity period). Ethereum gas is at 10 gwei.
2. User calls `eip_1559_transaction_price` (query) → receives stale estimate of 10 gwei (e.g., `max_transaction_fee = 0.00065 ETH`).
3. User calls `icrc2_approve` on the ckETH ledger, approving the minter for exactly `0.00065 ETH`.
4. Gas prices spike to 20 gwei on Ethereum.
5. User calls `withdraw_erc20`.
6. Minter calls `lazy_refresh_gas_fee_estimate()` → cache is >60 seconds old → fetches fresh fee history → new `max_transaction_fee = 0.0013 ETH`.
7. Minter calls `burn_from(cketh_account, 0.0013 ETH, ...)` → user's approval is only `0.00065 ETH`.
8. ckETH ledger returns `InsufficientAllowance { allowance: 0.00065 ETH, failed_burn_amount: 0.0013 ETH }`.
9. `withdraw_erc20` returns `WithdrawErc20Error::CkEthLedgerError { error: LedgerError::InsufficientAllowance }`.
10. Withdrawal fails. User must re-approve with a higher amount.

This is confirmed by the existing test `should_error_when_minter_not_allowed_to_burn_cketh` in `rs/ethereum/cketh/minter/tests/ckerc20.rs` which demonstrates the `InsufficientAllowance` path, and `should_refresh_gas_fee_estimate_only_once_within_a_minute` which confirms the 60-second cache boundary. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L169-198)
```rust
#[query]
async fn eip_1559_transaction_price(
    token: Option<Eip1559TransactionPriceArg>,
) -> Eip1559TransactionPrice {
    let gas_limit = match token {
        None => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        Some(Eip1559TransactionPriceArg { ckerc20_ledger_id }) => {
            match read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id)) {
                Some(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
                None => {
                    if ckerc20_ledger_id == read_state(|s| s.cketh_ledger_id) {
                        CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT
                    } else {
                        ic_cdk::trap(format!(
                            "ERROR: Unsupported ckERC20 token ledger {ckerc20_ledger_id}"
                        ))
                    }
                }
            }
        }
    };
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((ts, estimate)) => {
            let mut result = Eip1559TransactionPrice::from(estimate.to_price(gas_limit));
            result.timestamp = Some(ts);
            result
        }
        None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-542)
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
                }
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
                }
            }
        }
        Err(cketh_burn_error) => Err(WithdrawErc20Error::CkEthLedgerError {
            error: cketh_burn_error.into(),
        }),
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-681)
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
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L712-713)
```text
    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;
```

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L62-67)
```rust
    InsufficientAllowance {
        allowance: Nat,
        failed_burn_amount: Nat,
        token_symbol: String,
        ledger_id: Principal,
    },
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L241-241)
```text
1. The user calls the ckETH ledger to approve the minter to burn some of the user's ckETH tokens to pay for the transaction fees. The exact amount of ckETH needed depends on the current Ethereum gas price, which can greatly fluctuate. The following example approves the minter for 1 ETH, which could potentially allow for multiple withdrawals without having to approve the minter each time.
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L291-313)
```rust
    #[test]
    fn should_error_when_minter_not_allowed_to_burn_cketh() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let caller = ckerc20.caller();
        let cketh_ledger = ckerc20.cketh_ledger_id();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");

        ckerc20
            .call_minter_withdraw_erc20(
                caller,
                0_u8,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(WithdrawErc20Error::CkEthLedgerError {
                error: LedgerError::InsufficientAllowance {
                    allowance: Nat::from(0_u8),
                    failed_burn_amount: DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE.into(),
                    token_symbol: "ckETH".to_string(),
                    ledger_id: cketh_ledger,
                },
            });
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L533-582)
```rust
    fn should_refresh_gas_fee_estimate_only_once_within_a_minute() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
        let cketh_ledger = ckerc20.cketh_ledger_id();
        let user_1 = ckerc20.caller();
        let user_2: Principal = PrincipalId::new_user_test_id(DEFAULT_PRINCIPAL_ID + 1).into();
        assert_ne!(user_1, user_2);
        let insufficient_allowance_error = WithdrawErc20Error::CkEthLedgerError {
            error: LedgerError::InsufficientAllowance {
                allowance: Nat::from(0_u8),
                failed_burn_amount: DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE.into(),
                token_symbol: "ckETH".to_string(),
                ledger_id: cketh_ledger,
            },
        };

        let ckerc20 = ckerc20
            .call_minter_withdraw_erc20(
                user_1,
                0_u8,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(insufficient_allowance_error.clone());

        ckerc20.env.advance_time(Duration::from_secs(59));

        let ckerc20 = ckerc20
            .call_minter_withdraw_erc20(
                user_2,
                0_u8,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_no_refresh_gas_fee_estimate()
            .expect_error(insufficient_allowance_error.clone());

        ckerc20.env.advance_time(Duration::from_millis(1_001));

        ckerc20
            .call_minter_withdraw_erc20(
                user_2,
                0_u8,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(insufficient_allowance_error);
    }
```
