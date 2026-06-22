### Title
Hardcoded Gas Limits in ckETH Minter Cause Permanent Denial-of-Service for Smart Contract Wallet Withdrawals and Guaranteed Gas Fee Loss - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary
The ckETH minter canister uses hardcoded gas limits (`21_000` for ETH withdrawals, `65_000` for ERC20 withdrawals) when constructing Ethereum transactions. These limits are insufficient for smart contract wallet destinations (e.g., Gnosis Safe, multisig wallets), causing Ethereum transactions to fail with out-of-gas errors. Users permanently lose the gas fee on every failed attempt, and smart contract wallet users are permanently unable to withdraw ckETH/ckERC20 to their wallets.

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are hardcoded as compile-time constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit()` function returns these constants unconditionally with no dynamic estimation:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This gas limit is then passed directly into `create_transaction()` and embedded in the signed EIP-1559 transaction sent to Ethereum: [3](#0-2) 

The `eip_1559_transaction_price` query endpoint also uses these same hardcoded limits to estimate fees for users, meaning users are quoted a fee based on 21,000 gas even when their destination requires more: [4](#0-3) 

**Root cause:** `21_000` gas is the exact minimum for a simple ETH transfer to an EOA. Any smart contract wallet with a `receive()` or `fallback()` function requires additional gas beyond this limit. When the transaction is submitted to Ethereum with `gas_limit = 21_000`, it immediately runs out of gas and reverts.

**Reimbursement does not make users whole:** When a ckETH withdrawal transaction fails on Ethereum, the minter reimburses `withdrawal_amount - effective_fee_paid` (i.e., the user permanently loses the gas fee): [5](#0-4) 

For ckERC20 failures, only the ckERC20 tokens are reimbursed — the ckETH gas fee is **not** reimbursed: [6](#0-5) 

The documentation itself acknowledges the `65_000` limit is an assumption: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [7](#0-6) 

### Impact Explanation

1. **Permanent DoS for smart contract wallet users:** Any user whose Ethereum destination is a smart contract wallet (Gnosis Safe, multisig, proxy wallet, etc.) will have every ckETH withdrawal transaction fail on Ethereum. The transaction is constructed with `gas_limit = 21_000`, which is insufficient for any contract's `receive()` function. The user can never successfully withdraw ckETH to their smart contract wallet.

2. **Guaranteed gas fee loss per failed attempt:** Each failed withdrawal costs the user `gas_used * effective_gas_price` in ckETH. From the integration test, a single failed withdrawal costs approximately `693,077,873,418,000` wei (~0.000693 ETH at test gas prices). A user repeatedly attempting to withdraw to their smart contract wallet loses this fee on every attempt. [8](#0-7) 

3. **ckERC20 gas fee loss without reimbursement:** For ckERC20 withdrawals to smart contract wallets or complex ERC20 tokens (fee-on-transfer, ERC777 hooks, rebasing tokens), the ckETH gas fee is burned and never returned even when the Ethereum transaction fails.

4. **Incorrect fee estimates:** The `eip_1559_transaction_price` query returns fee estimates based on 21,000 gas, misleading smart contract wallet users into thinking their withdrawal will succeed at that cost.

### Likelihood Explanation

Smart contract wallets (Gnosis Safe, Argent, etc.) are widely used in DeFi. Any ckETH/ckERC20 holder whose Ethereum address is a smart contract wallet — a common pattern for DAOs, institutional users, and security-conscious individuals — will be permanently affected. The entry path requires only a standard unprivileged call to `withdraw_eth` or `withdraw_erc20` on the minter canister with a smart contract wallet as the recipient. No special privileges or coordination are required. The likelihood is **medium-high** given the prevalence of smart contract wallets in the Ethereum ecosystem.

### Recommendation

Replace the hardcoded constants with a dynamic gas estimation mechanism. Before constructing the withdrawal transaction, the minter should call `eth_estimateGas` via the EVM RPC canister to obtain an accurate gas limit for the specific destination address. A safety multiplier (e.g., 1.2×) should be applied to the estimate to account for gas cost variability. If dynamic estimation is not feasible, the hardcoded limits should at minimum be raised significantly (e.g., 100,000 for ETH, 200,000 for ERC20) to accommodate smart contract wallets, and the limits should be made configurable via upgrade arguments rather than compile-time constants.

### Proof of Concept

1. User holds ckETH and controls a Gnosis Safe at Ethereum address `0xSafeAddress`.
2. User calls `withdraw_eth` on the ckETH minter canister specifying `recipient = "0xSafeAddress"` and `amount = X`.
3. The minter burns `X` ckETH from the user's ledger account.
4. `estimate_gas_limit()` returns `GasAmount::new(21_000)` unconditionally.
5. `create_transaction()` constructs an EIP-1559 transaction with `gas_limit: CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` (21,000).
6. The transaction is signed via tECDSA and submitted to Ethereum.
7. The Gnosis Safe's `receive()` function requires >21,000 gas (Gnosis Safe's fallback handler alone costs ~6,000+ gas on top of the base 21,000). The transaction reverts with out-of-gas.
8. The minter observes `TransactionStatus::Failure` in the receipt and schedules a reimbursement of `withdrawal_amount - effective_fee_paid`.
9. The user receives back their ckETH minus the gas fee (~0.000693 ETH equivalent at current prices).
10. Steps 2–9 repeat indefinitely; the user can never withdraw ckETH to their Gnosis Safe and loses gas fees on every attempt.

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L170-198)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L719-731)
```rust
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-746)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L510-516)
```rust
    let cost_of_failed_transaction = withdrawal_amount
        .0
        .to_u128()
        .unwrap()
        .checked_sub(tx.value.unwrap().as_u128())
        .unwrap();
    assert_eq!(cost_of_failed_transaction, 693_077_873_418_000);
```
