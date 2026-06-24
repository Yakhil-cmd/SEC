### Title
Hardcoded Gas Limit for ckERC20 Withdrawals Causes Irrecoverable ckETH Gas Fee Loss on Out-of-Gas Failure - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes the Ethereum gas limit for all ckERC20 withdrawal transactions to `65_000`. If a supported ERC-20 token's `transfer` function consumes more gas than this limit (e.g., due to token-level hooks, fee-on-transfer logic, or contract upgrades), the outbound Ethereum transaction fails with an out-of-gas error. Upon failure, the ckERC20 tokens are reimbursed to the user, but the ckETH gas fee — which was pre-burned at withdrawal time — is **permanently lost with no reimbursement path**. The user has no mechanism to specify a higher gas limit.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are hardcoded as constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns one of these two constants based solely on the withdrawal type, with no consideration of the specific ERC-20 token or any user-supplied value:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This value is passed directly into `create_transactions_batch`, which calls `create_transaction` with the hardcoded limit: [3](#0-2) 

The `create_transaction` function for `CkErc20` uses this `gas_limit` directly in the outbound EIP-1559 transaction: [4](#0-3) 

The `withdraw_erc20` endpoint in `main.rs` accepts no gas limit parameter from the user. The ckETH gas fee is burned upfront at withdrawal time: [5](#0-4) 

The `Erc20WithdrawalRequest` struct stores only `max_transaction_fee` (a Wei budget), not a gas limit: [6](#0-5) 

The critical asymmetry in the reimbursement policy is documented explicitly:

> "The minter retrieves the receipt of the finalized transaction and will reimburse the ckERC20 tokens in case the transaction failed. **Overcharged transaction fees are not reimbursed.**" [7](#0-6) 

This is confirmed in the test `should_reimburse_tokens_when_ckerc20_withdrawal_fails`, which shows ckERC20 tokens are reimbursed but the ckETH gas fee entry is absent from reimbursement requests: [8](#0-7) 

The `eip_1559_transaction_price` query endpoint also hardcodes the gas limit returned to users for fee estimation: [9](#0-8) 

---

### Impact Explanation

When a supported ERC-20 token's `transfer` function requires more than `65_000` gas (e.g., due to token-level transfer hooks, fee-on-transfer mechanics, rebasing logic, or a contract upgrade after governance approval), the minter's outbound Ethereum transaction fails with an out-of-gas error. The consequences are:

1. The ckERC20 tokens are reimbursed to the user (correct).
2. The ckETH gas fee — pre-burned at withdrawal time and stored as `max_transaction_fee` in the `Erc20WithdrawalRequest` — is **permanently lost**. There is no code path that reimburses it on transaction failure.

The default `max_transaction_fee` used in tests is `30_000_000_000_000_000` wei (~0.03 ETH), a non-trivial amount. The user has no ability to specify a higher gas limit to avoid this outcome.

---

### Likelihood Explanation

The likelihood is **low-to-medium**. Only governance-approved ERC-20 tokens are supported, providing a first line of defense. However:

- ERC-20 token contracts are upgradeable; a token approved at 50,000 gas could be upgraded to require 80,000 gas.
- Some legitimate ERC-20 tokens (e.g., USDT with its fee logic, tokens with transfer hooks like ERC-777 wrappers, or tokens with on-chain allowlist checks) may already exceed 65,000 gas in certain states.
- The `65_000` limit is acknowledged as an assumption ("should be sufficient for standard ERC-20 contracts"), not a guarantee.

Any unprivileged user calling `withdraw_erc20` for such a token triggers the loss.

---

### Recommendation

1. Allow users to optionally specify a `gas_limit` in the `withdraw_erc20` call, bounded by a protocol-defined maximum, analogous to how `max_transaction_fee` is already user-specified.
2. Alternatively, implement a reimbursement path for the ckETH gas fee when the Ethereum transaction fails with `TransactionStatus::Failure` due to out-of-gas (detectable via `gas_used == gas_limit` in the receipt).
3. At minimum, validate at `add_ckerc20_token` time that the token's `transfer` function fits within the hardcoded gas budget.

---

### Proof of Concept

1. A supported ERC-20 token (governance-approved) has a `transfer` function that requires `>65_000` gas (e.g., after a contract upgrade or due to complex transfer logic).
2. User calls `withdraw_erc20` specifying `max_transaction_fee` (e.g., 0.03 ETH worth of ckETH).
3. Minter burns `max_transaction_fee` ckETH from the user's account (irreversible).
4. Minter burns the ckERC20 withdrawal amount from the user's account.
5. `estimate_gas_limit` returns `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`.
6. `create_transaction` builds an EIP-1559 transaction with `gas_limit = 65_000`.
7. The Ethereum transaction is submitted and fails with out-of-gas (`gas_used == gas_limit`).
8. `record_finalized_transaction` detects `TransactionStatus::Failure` and schedules reimbursement of the ckERC20 tokens only.
9. `process_reimbursement` mints back the ckERC20 tokens to the user.
10. The ckETH gas fee (`max_transaction_fee`) is never reimbursed — the user permanently loses it. [1](#0-0) [10](#0-9) [4](#0-3) [11](#0-10)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L173-198)
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
}
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-275)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
. The minter attempts to burn the specified token amount from the user account on the ckERC20 ledger. If the burn succeeds, the minter schedules a withdrawal task. If the burn fails (e.g., insufficient funds), the minter schedules the reimbursement of the burnt ckETH amount from the previous step minus some (small) penalty fee.
. The ckETH minter constructs a 0-ETH amount transaction containing the ERC-20 withdrawal (in `data` field) to the Ethereum network.
. The user can query the withdrawal status using the identifier from the erc20_withdraw response.
. Once the transaction gets enough confirmations, the minter considers the transaction finalized.
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1642-1687)
```rust
        #[test]
        fn should_reimburse_tokens_when_ckerc20_withdrawal_fails() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = ckerc20_withdrawal_request_with_index(
                cketh_ledger_burn_index,
                ckerc20_ledger_burn_index,
            );
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(40_000_u32),
                effective_gas_price: WeiPerGas::from(100_u16),
                ..transaction_receipt(&signed_tx, TransactionStatus::Failure)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                Wei::from(4_000_000_u32)
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());
            let expected_ckerc20_reimbursed_amount = withdrawal_request.withdrawal_amount;

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(
                transactions.reimbursement_requests,
                btreemap! {
                    ReimbursementIndex::CkErc20 {
                        cketh_ledger_burn_index,
                        ledger_id: withdrawal_request.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index } =>
                    ReimbursementRequest {
                        ledger_burn_index: cketh_ledger_burn_index,
                        reimbursed_amount: expected_ckerc20_reimbursed_amount.change_units(),
                        to: withdrawal_request.from,
                        to_subaccount: withdrawal_request.from_subaccount,
                        transaction_hash: Some(receipt.transaction_hash),
                    }
                }
            );
        }
```
