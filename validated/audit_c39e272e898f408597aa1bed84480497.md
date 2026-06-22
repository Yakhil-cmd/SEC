### Title
ckERC20 Minter Uses Single Global Gas Limit for All ERC20 Tokens, Causing Irrecoverable ckETH Loss on Out-of-Gas Failures - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

### Summary
The ckETH minter applies a single hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000` to every ckERC20 withdrawal, regardless of the specific ERC20 contract's actual gas requirements. This is the direct IC analog of H-03: a global parameter is used where a per-destination value is required. When an ERC20 contract consumes more than 65,000 gas, the Ethereum transaction fails out-of-gas; the ckERC20 tokens are reimbursed but the ckETH burned for the transaction fee is permanently lost.

### Finding Description
In `rs/ethereum/cketh/minter/src/withdraw.rs`, two constants are declared and `estimate_gas_limit` returns the same value for every ckERC20 token regardless of which contract is the destination:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);

pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_)   => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [1](#0-0) [2](#0-1) 

This constant flows directly into `create_transactions_batch`, which calls `estimate_gas_limit` and passes the result to `create_transaction`: [3](#0-2) 

`create_transaction` embeds the gas limit verbatim into the signed EIP-1559 transaction sent to Ethereum: [4](#0-3) 

The public query `eip_1559_transaction_price` also returns a fee estimate based on the same constant for every ckERC20 token, so users cannot distinguish tokens with higher gas requirements: [5](#0-4) 

The documentation explicitly acknowledges the limitation: *"The gas_limit for ckERC20 withdrawals is currently fixed to 65_000 and should be sufficient for standard ERC-20 contracts."* [6](#0-5) 

### Impact Explanation
When an ERC20 contract requires more than 65,000 gas for a `transfer` call (e.g., tokens with transfer hooks, fee-on-transfer logic, or rebasing mechanics), the Ethereum transaction reverts out-of-gas. The minter detects the `Failure` status in the receipt and reimburses the ckERC20 tokens. However, the ckETH burned upfront to pay the transaction fee is **not** reimbursed — the documentation states "Overcharged transaction fees are not reimbursed." The user suffers a permanent loss of ckETH equal to `max_fee_per_gas × 65_000`, which at typical Ethereum gas prices can be tens to hundreds of USD per failed withdrawal.

### Likelihood Explanation
The NNS has already added USDT (Tether), whose non-standard transfer logic consumes 63,000–65,000 gas — right at the boundary. Any of the following realistic events triggers the loss:
1. A supported token contract is upgraded (USDT has been upgraded before) to consume slightly more gas.
2. A new ckERC20 token is added whose contract has transfer hooks or fee logic exceeding 65,000 gas.
3. Contract storage slot initialization on first transfer to a new address pushes gas above the limit.

An unprivileged user triggers the path simply by calling `withdraw_erc20` for any such token; no privileged access is required.

### Recommendation
1. Store a per-token `gas_limit` field alongside each supported ckERC20 token in the minter state, set at token-addition time via the NNS proposal.
2. Expose an NNS-gated update path to revise the gas limit for existing tokens.
3. Raise the default to a safer value (e.g., 100,000) to provide headroom for non-standard contracts.
4. Consider reimbursing the ckETH fee when the transaction fails due to out-of-gas (distinguishable from a logic revert via `gasUsed == gasLimit` in the receipt).

### Proof of Concept
1. User calls `withdraw_erc20` for a ckERC20 token whose underlying ERC20 contract requires >65,000 gas per transfer.
2. Minter burns ckETH (e.g., `max_fee_per_gas × 65_000`) and ckERC20 tokens.
3. Minter constructs an EIP-1559 transaction with `gas_limit = 65_000` and broadcasts it.
4. Ethereum executes the transaction; it reverts out-of-gas.
5. Minter reads the `Failure` receipt, reimburses ckERC20 tokens, but does **not** reimburse ckETH.
6. User's ckETH balance is permanently reduced by the transaction fee.

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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```
