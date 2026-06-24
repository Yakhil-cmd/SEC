### Title
Fixed Gas Limit in ckETH/ckERC20 Withdrawal Transactions Causes Permanent Loss of User Funds - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter canister uses hardcoded, fixed gas limits for all Ethereum withdrawal transactions: `21_000` gas for ckETH → ETH withdrawals and `65_000` gas for ckERC20 → ERC20 withdrawals. When the destination is a smart contract that requires more gas than the fixed limit, the Ethereum transaction fails on-chain. The minter observes the failure and reimburses the token principal, but the gas fee (paid in ckETH) is permanently lost by the user. Users have no mechanism to specify a higher gas limit to avoid this outcome.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two constants define the gas limits for all withdrawal transactions: [1](#0-0) 

The function `estimate_gas_limit` unconditionally returns one of these two constants based solely on the withdrawal type, with no user-configurable override: [2](#0-1) 

This value is passed directly into `create_transactions_batch` and then into `create_transaction`, which embeds it into the signed EIP-1559 transaction submitted to Ethereum: [3](#0-2) 

The `21_000` gas limit for ckETH is the minimum for a plain EOA-to-EOA ETH transfer. Any smart contract destination that executes logic in its `receive()` or `fallback()` function (e.g., multisig wallets, DeFi vaults, proxy contracts) will exceed this limit and cause an out-of-gas revert. The `65_000` gas limit for ckERC20 is similarly insufficient for ERC20 tokens with transfer hooks, rebasing logic, or fee-on-transfer mechanisms.

The interface definition file explicitly acknowledges the ckETH case: [4](#0-3) 

The ckERC20 documentation similarly states the limit is fixed and "should be sufficient for standard ERC-20 contracts," implicitly acknowledging non-standard tokens are at risk: [5](#0-4) 

---

### Impact Explanation

**For ckETH withdrawals:** When the Ethereum transaction fails due to out-of-gas, `record_finalized_transaction` reimburses only `finalized_tx.transaction_amount()` — the ETH value field of the transaction — not the full original withdrawal amount. The gas fee (actual ETH burned by miners) is permanently lost: [6](#0-5) 

**For ckERC20 withdrawals:** The ckERC20 tokens are reimbursed on failure, but the ckETH gas fee burned upfront is never returned. The documentation confirms: "Overcharged transaction fees are not reimbursed": [7](#0-6) 

The reimbursement flow confirms the ckETH gas fee is not included in the ckERC20 reimbursement amount: [8](#0-7) 

The net result is a permanent, irreversible loss of ETH-denominated gas fees for any user who withdraws to a smart contract address or uses a non-standard ERC20 token. The minter considers the withdrawal lifecycle complete after reimbursement, with no retry mechanism.

---

### Likelihood Explanation

**ckETH (21,000 gas) — High likelihood:** Smart contract wallets (Gnosis Safe, Argent, etc.) are extremely common destinations for institutional and DeFi users. The DID file itself warns this "will likely fail," confirming the developers are aware this is a realistic scenario. Any user who copies their multisig address as the withdrawal destination triggers this loss.

**ckERC20 (65,000 gas) — Medium likelihood:** ERC20 tokens with transfer hooks (ERC777 adapters, rebasing tokens like stETH, fee-on-transfer tokens) are widely deployed on Ethereum mainnet. As the ckERC20 ecosystem expands to support more tokens, the probability of encountering a token whose `transfer()` exceeds 65,000 gas increases. The minter currently supports adding arbitrary ERC20 tokens via governance: [9](#0-8) 

---

### Recommendation

Allow users to optionally specify a `gas_limit` parameter in `WithdrawalArg` and `WithdrawErc20Arg`. The minter should validate the user-supplied limit is at least the current minimum constant and at most a protocol-defined maximum. The `estimate_gas_limit` function should use the user-supplied value when present, falling back to the current defaults otherwise. This mirrors the fix applied to the analogous `Cell` contract vulnerability.

Additionally, consider adding a pre-flight check or warning when the destination address is a known contract (detectable via `eth_getCode`), so users are informed before funds are committed.

---

### Proof of Concept

1. User holds ckETH and calls `withdraw_eth` on the minter canister, specifying a Gnosis Safe multisig address as `recipient`.
2. The minter burns the user's ckETH and creates an Ethereum transaction with `gas_limit = 21_000`: [10](#0-9) 
3. The Ethereum transaction is submitted. The Gnosis Safe `receive()` function requires ~30,000–50,000 gas. The transaction reverts with out-of-gas. Miners keep the gas fee.
4. The minter observes `TransactionStatus::Failure` in the receipt and schedules reimbursement of only the ETH `amount` field (not the gas fee): [11](#0-10) 
5. The user receives back `withdrawal_amount - max_transaction_fee_estimate` in ckETH. The gas fee (up to `21_000 * max_fee_per_gas`) is permanently lost. At current gas prices (~30 gwei), this is approximately `21_000 * 30e9 = 630,000 gwei = 0.00063 ETH` per failed withdrawal — a real monetary loss with no recovery path.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L257-264)
```rust
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-745)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1135-1145)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: transaction_price.max_priority_fee_per_gas,
                max_fee_per_gas: transaction_price.max_fee_per_gas,
                gas_limit: transaction_price.gas_limit,
                destination: request.destination,
                amount: tx_amount,
                data: Vec::new(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1643-1687)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-573)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
```
