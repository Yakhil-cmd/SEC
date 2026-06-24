### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Causes Systematic ckETH Fee Overpayment and Potential OOG-Induced Fee Loss in ckERC20 Withdrawals - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

---

### Summary

The ckETH minter hardcodes the Ethereum gas limit for **all** ckERC20 withdrawal transactions to `65_000` gas, regardless of the actual gas consumption of the specific ERC-20 token. This causes users to systematically overpay ckETH fees (which are explicitly not reimbursed by design), and can cause permanent ckETH fee loss when a supported token's `transfer()` call exceeds 65,000 gas and the transaction fails out-of-gas on Ethereum.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, the constant `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is hardcoded to `GasAmount::new(65_000)` for every ckERC20 token: [1](#0-0) 

This single value is used in two critical, coupled places:

**1. Fee burned from the user's ckETH account** (`estimate_erc20_transaction_fee`): [2](#0-1) 

The burned amount is `max_fee_per_gas × 65,000`, regardless of the token.

**2. Actual gas limit placed in the Ethereum transaction** (`estimate_gas_limit`): [3](#0-2) 

This gas limit is then passed directly into `create_transaction` and embedded in the signed EIP-1559 transaction sent to Ethereum: [4](#0-3) 

The `eip_1559_transaction_price` query endpoint, which users call to determine how much ckETH to approve, also returns a price estimate computed against this same hardcoded 65,000 gas limit: [5](#0-4) 

The documentation acknowledges the hardcoding explicitly: [6](#0-5) 

The documentation also states that overcharged fees are not reimbursed: [7](#0-6) 

---

### Impact Explanation

**Overpayment (systematic, always occurs):** Standard ERC-20 `transfer()` calls consume far less than 65,000 gas in practice. For example, USDC uses approximately 35,000–45,000 gas per transfer. The minter burns `max_fee_per_gas × 65,000` ckETH from the user, but the Ethereum network only charges `effective_gas_price × actual_gas_used`. The difference — `max_fee_per_gas × (65,000 − actual_gas_used)` — is permanently lost to the user. At 30 gwei max fee and 35,000 actual gas, this is `30 gwei × 30,000 = 900,000 gwei ≈ 0.0009 ETH` per withdrawal, with no reimbursement path.

**Underpayment / OOG failure (token-dependent):** For ERC-20 tokens whose `transfer()` function exceeds 65,000 gas (e.g., tokens with fee-on-transfer logic, rebasing mechanisms, or complex transfer hooks), the Ethereum transaction will fail with an out-of-gas error. In this case, the ckERC20 tokens are reimbursed, but the ckETH fee burned upfront is **not** reimbursed (minus a penalty deduction). The user suffers a direct, permanent ckETH loss. [8](#0-7) 

---

### Likelihood Explanation

**Overpayment:** High. Every ckERC20 withdrawal where actual gas consumption is below 65,000 results in a non-reimbursable ckETH overcharge. This applies to the majority of standard ERC-20 tokens currently supported (USDC, USDT, etc.).

**OOG failure:** Medium. The minter controls which tokens are whitelisted, but the 65,000 limit is a single global constant applied to all tokens. Any future addition of a token with complex transfer logic (e.g., tokens with hooks, rebasing tokens, or tokens with on-chain fee mechanisms) that exceeds 65,000 gas will silently cause OOG failures and permanent ckETH fee loss for users.

---

### Recommendation

Replace the single global `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` constant with a per-token gas limit stored in the minter's state alongside each supported ckERC20 token. The gas limit for each token should be set when the token is added via `add_ckerc20_token` and should be updatable via governance upgrade. This mirrors how the `eip_1559_transaction_price` endpoint already dispatches on token type: [9](#0-8) 

The `AddCkErc20Token` message and the `CkErc20Token` state struct should be extended with an optional `gas_limit` field, defaulting to 65,000 for backward compatibility but allowing per-token overrides.

---

### Proof of Concept

1. User queries `eip_1559_transaction_price(opt record { ckerc20_ledger_id = USDC_LEDGER })` → receives fee estimate computed as `max_fee_per_gas × 65,000`.
2. User calls `icrc2_approve` on the ckETH ledger to allow the minter to burn `max_transaction_fee` ckETH.
3. User calls `withdraw_erc20` with their USDC amount.
4. Minter calls `estimate_erc20_transaction_fee()` → burns `max_fee_per_gas × 65,000` ckETH from the user.
5. Minter calls `estimate_gas_limit(&request)` → returns `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65,000`.
6. Minter submits an Ethereum transaction with `gas_limit = 65,000`.
7. USDC `transfer()` consumes ~35,000 gas → transaction succeeds, but user permanently lost `max_fee_per_gas × 30,000` ckETH with no reimbursement path.
8. Alternatively, for a token consuming >65,000 gas → transaction fails OOG → ckERC20 reimbursed, ckETH fee permanently lost. [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-293)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        log!(DEBUG, "[create_transactions_batch]: processing {request:?}",);
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
            Ok(transaction) => {
                log!(
                    DEBUG,
                    "[create_transactions_batch]: created transaction {transaction:?}",
                );

                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::CreatedTransaction {
                            withdrawal_id: request.cketh_ledger_burn_index(),
                            transaction,
                        },
                    );
                });
            }
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
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L296-301)
```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L173-197)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-513)
```rust
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1184)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
                gas_limit,
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
        }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
