### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Permanently Blocks Withdrawals for High-Gas ERC-20 Tokens - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter applies a single hardcoded gas limit of `65_000` to every ckERC20 withdrawal transaction, regardless of the specific ERC-20 token being withdrawn. Any supported ckERC20 token whose on-chain `transfer()` execution requires more than 65,000 gas will have every withdrawal transaction fail with out-of-gas on Ethereum. Because the ckETH gas fee is burned upfront and explicitly not reimbursed on failure, each failed attempt permanently destroys the user's ckETH. The withdrawal itself is permanently blocked for that token.

---

### Finding Description

`CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is a compile-time constant set to `65_000`: [1](#0-0) 

The function `estimate_gas_limit` returns this constant unconditionally for every ckERC20 withdrawal, with no per-token override: [2](#0-1) 

This value is used in `create_transactions_batch` to build the EIP-1559 transaction sent to Ethereum: [3](#0-2) 

The same constant is used in `estimate_erc20_transaction_fee` (which determines how much ckETH is burned from the user upfront) and in `eip_1559_transaction_price` (the public price-query endpoint): [4](#0-3) [5](#0-4) 

The `CkErc20Token` struct stores no per-token gas limit field, so there is no mechanism to configure a higher limit for a specific token: [6](#0-5) 

The project documentation explicitly acknowledges the hardcoded limit and its assumption:

> "The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and **should be sufficient for standard ERC-20 contracts**." [7](#0-6) 

The same documentation confirms that overcharged (i.e., burned) ckETH transaction fees are **not** reimbursed when the Ethereum transaction fails:

> "The minter retrieves the receipt of the finalized transaction … and will reimburse the ckERC20 tokens in case the transaction failed. **Overcharged transaction fees are not reimbursed.**" [8](#0-7) 

The reimbursement code confirms this: on a failed Ethereum transaction, only the ckERC20 token amount is minted back; the ckETH gas fee is consumed: [9](#0-8) 

---

### Impact Explanation

For any supported ckERC20 token whose `transfer(address,uint256)` execution on Ethereum consumes more than 65,000 gas (e.g., tokens with transfer hooks, rebasing logic, fee-on-transfer mechanics, or complex storage layouts):

1. The user calls `withdraw_erc20`. The minter burns ckETH for the gas fee and burns the ckERC20 amount.
2. The minter submits an Ethereum transaction with `gas_limit = 65_000`.
3. The transaction reverts on Ethereum with out-of-gas.
4. The minter reimburses the ckERC20 tokens but **does not reimburse the ckETH gas fee**.
5. Every subsequent withdrawal attempt repeats steps 1–4 identically, permanently blocking withdrawals for that token and draining ckETH from every user who tries.

This is a **chain-fusion burn/withdrawal blockage**: the bridge is permanently broken for the affected token, and users suffer irreversible ckETH loss on each attempt.

---

### Likelihood Explanation

The ckERC20 system is designed to support any ERC-20 token added via NNS governance proposal. The governance process does not enforce a gas-usage check. Many production ERC-20 tokens require more than 65,000 gas for a `transfer` call (e.g., USDT on mainnet historically required ~65,000 and can exceed it; tokens with ERC-777 hooks, fee-on-transfer tokens, or tokens with complex allowance/balance storage patterns routinely exceed this). Any unprivileged user holding a ckERC20 token whose underlying contract exceeds the limit can trigger the failure simply by calling `withdraw_erc20`.

---

### Recommendation

1. Add an optional `erc20_gas_limit: Option<GasAmount>` field to `CkErc20Token` so that tokens with higher gas requirements can be registered with a per-token override.
2. Update `estimate_gas_limit` to use the per-token value when present, falling back to `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT`.
3. Update `add_ckerc20_token` and the `AddCkErc20Token` endpoint to accept and persist this optional field.
4. Consider reimbursing at least a portion of the ckETH gas fee when the Ethereum transaction fails due to out-of-gas, to avoid penalizing users for a protocol-level misconfiguration.

---

### Proof of Concept

**Entry path (unprivileged ingress):**

1. A governance proposal adds a ckERC20 token whose underlying ERC-20 `transfer()` requires, say, 80,000 gas.
2. User calls `withdraw_erc20` on the ckETH minter canister (`p5jb7-...`).
3. Minter burns `erc20_tx_fee` ckETH from the user (computed using `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`).
4. Minter burns the ckERC20 withdrawal amount.
5. Minter constructs and signs an EIP-1559 transaction with `gas_limit = 65_000` via `create_transactions_batch` → `estimate_gas_limit` → `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT`.
6. Ethereum miners execute the transaction; it runs out of gas at ~65,000 and reverts.
7. Minter observes `TransactionStatus::Failure` in the finalized receipt.
8. Minter reimburses ckERC20 tokens only; ckETH gas fee is permanently lost.
9. User retries → same outcome, more ckETH lost.

The hardcoded constant at `rs/ethereum/cketh/minter/src/withdraw.rs:44` is the sole, necessary root cause. No privileged access, no threshold attack, and no external oracle is required. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-264)
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L173-188)
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

**File:** rs/ethereum/cketh/minter/src/erc20.rs (L18-28)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Decode, Encode)]
pub struct CkErc20Token {
    #[n(0)]
    pub erc20_ethereum_network: EthereumNetwork,
    #[n(1)]
    pub erc20_contract_address: Address,
    #[n(2)]
    pub ckerc20_token_symbol: CkTokenSymbol,
    #[cbor(n(3), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
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
