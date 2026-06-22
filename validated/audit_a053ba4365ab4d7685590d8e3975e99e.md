### Title
Fixed `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` of 65,000 May Cause ERC-20 Withdrawal Failures with Permanent ckETH Fee Loss - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary

The ckETH minter hardcodes `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000` for every ckERC20 withdrawal transaction sent to Ethereum. If a supported ERC-20 token's `transfer()` function consumes more than 65,000 gas (due to complex token logic, cold storage slots, fee-on-transfer mechanics, or blacklist checks), the Ethereum transaction reverts with "out of gas." On revert, the minter reimburses only the ckERC20 withdrawal amount; the ckETH burned upfront to pay the transaction fee is permanently lost and never reimbursed. This is the IC chain-fusion analog of the Optimism bridge `l2Gas` underpayment issue: a fixed/insufficient gas budget for a cross-chain operation causes the operation to fail and user funds to be lost.

### Finding Description

**Root cause — hardcoded gas limit:** [1](#0-0) 

`CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is a compile-time constant of `65_000`. It is applied uniformly to every ckERC20 withdrawal regardless of which ERC-20 token is being transferred. [2](#0-1) 

`estimate_gas_limit` returns this constant for all `CkErc20` requests with no per-token override.

**How the transaction is built:** [3](#0-2) 

The `gas_limit` field of the EIP-1559 transaction is set directly from the constant. There is no dynamic estimation or per-token adjustment.

**ckETH fee is burned before the Ethereum transaction is sent:** [4](#0-3) 

`estimate_erc20_transaction_fee()` is called and the resulting `erc20_tx_fee` is immediately burned from the user's ckETH account via `burn_from`. This burn is irreversible at this point.

**On Ethereum transaction failure, only ckERC20 tokens are reimbursed — ckETH fee is not:** [5](#0-4) 

`record_finalized_transaction` creates a `ReimbursementRequest` with `reimbursed_amount: request.withdrawal_amount.change_units()` — this is the ERC-20 token amount only. The `max_transaction_fee` (ckETH) field of the `Erc20WithdrawalRequest` is never included in any reimbursement.

This is explicitly documented as intentional: [6](#0-5) 

> "Overcharged transaction fees are not reimbursed."

**The gas limit assumption is acknowledged as approximate:** [7](#0-6) 

> "The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."

The qualifier "standard" is critical: non-standard or complex ERC-20 tokens (USDT with blacklisting, fee-on-transfer tokens, tokens with ERC-777-style hooks, tokens with complex storage layouts) routinely exceed 65,000 gas for a single `transfer()` call.

### Impact Explanation

When a supported ckERC20 token's `transfer()` requires more than 65,000 gas:

1. The user's ckETH is burned upfront (e.g., `max_transaction_fee` ≈ 30,000,000,000,000,000 wei = 0.03 ETH at current estimates).
2. The Ethereum transaction is submitted with `gas_limit = 65,000`.
3. The EVM exhausts all 65,000 gas units and reverts with "out of gas."
4. Ethereum charges the full `65,000 * effective_gas_price` fee to the minter's address.
5. The minter reimburses the ckERC20 withdrawal amount to the user.
6. The ckETH burned for the fee is **permanently lost** — no reimbursement path exists.

The user suffers a direct, irreversible loss of ckETH equal to `max_transaction_fee`. There is no retry mechanism with a higher gas limit; the user must initiate a new withdrawal and burn additional ckETH.

**Vulnerability class:** chain-fusion mint/burn/replay bug — insufficient gas for a cross-chain operation causes the operation to fail and user funds (ckETH fee) to be permanently lost.

### Likelihood Explanation

- **Entry path:** Any unprivileged IC principal can call `withdraw_erc20` on the ckETH minter canister — a fully public ingress endpoint.
- **Trigger condition:** A supported ERC-20 token whose `transfer()` function consumes ≥ 65,000 gas. Real-world examples include USDT (which has blacklist checks and can approach this threshold), fee-on-transfer tokens, and tokens with complex storage patterns. The USDC deposit example in the docs itself consumed 56,970 gas for a `depositErc20` call (which includes `approve` + `transferFrom`), placing a plain `transfer()` very close to the 65,000 limit.
- **No special privileges required:** The attacker is the victim — any user withdrawing a borderline token triggers the loss.
- **Likelihood:** Medium. The condition depends on which tokens are governance-approved. As the set of supported ckERC20 tokens grows, the probability of including a token that exceeds 65,000 gas increases.

### Recommendation

1. **Per-token gas limit configuration:** Allow governance to set a per-token `gas_limit` override when adding a new ckERC20 token, rather than using a single global constant.
2. **Dynamic gas estimation:** Before submitting the withdrawal transaction, use `eth_estimateGas` (via the EVM RPC canister) to estimate the actual gas required for the specific token and amount.
3. **Reimburse ckETH on out-of-gas failure:** Detect when `gas_used == gas_limit` in the receipt (a strong signal of out-of-gas) and reimburse the unused portion of `max_transaction_fee` minus the actual fee paid, rather than treating all failures identically.
4. **Increase the default limit conservatively:** Raise `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` to a safer value (e.g., 100,000–150,000) to accommodate a wider range of standard tokens.

### Proof of Concept

**Attacker-controlled entry path:**

1. User calls `withdraw_erc20` on the ckETH minter for a supported ckERC20 token (e.g., a token whose `transfer()` uses 70,000 gas).
2. Minter calls `estimate_erc20_transaction_fee()` → burns `erc20_tx_fee` ckETH from user's account.
3. Minter calls `burn_from` on the ckERC20 ledger → burns the withdrawal amount.
4. Minter calls `create_transaction` with `gas_limit = CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`.
5. Transaction is signed via threshold ECDSA and submitted to Ethereum.
6. EVM executes the ERC-20 `transfer()`, exhausts all 65,000 gas at step ~65,000, reverts.
7. Receipt arrives: `status = Failure`, `gas_used = 65,000` (= `gas_limit`).
8. `record_finalized_transaction` is called:
   - `ReimbursementRequest` is created for `withdrawal_amount` (ckERC20 tokens only).
   - `max_transaction_fee` (ckETH) is not included in any reimbursement.
9. `process_reimbursement` mints back the ckERC20 tokens to the user.
10. **Result:** User recovers their ckERC20 tokens but permanently loses their ckETH fee.

The code path is confirmed by the test `should_reimburse_tokens_when_ckerc20_withdrawal_fails`: [8](#0-7) 

This test shows that on `TransactionStatus::Failure`, only `withdrawal_amount` is reimbursed — the `max_transaction_fee` ckETH is silently consumed with no reimbursement path.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-458)
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
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
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
