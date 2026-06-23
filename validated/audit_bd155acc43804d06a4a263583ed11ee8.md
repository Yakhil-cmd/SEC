### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` of 65,000 Can Permanently Break ERC20 Withdrawals and Cause ckETH Fee Loss - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter uses a hardcoded gas limit of `65_000` for all ckERC20 withdrawal transactions on Ethereum. This is the direct IC analog of the Solidity `transfer`-vs-`call` issue: just as Solidity's `transfer` hardcodes 2300 gas and can break integrations when gas costs change, the minter's hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` can cause ERC20 withdrawal transactions to fail with out-of-gas for any token whose `transfer` function requires more than 65,000 gas. When this happens, the user loses the ckETH fee paid for the transaction (only the ckERC20 tokens are reimbursed, not the ckETH fee).

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are hardcoded as compile-time constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` for every ckERC20 withdrawal, regardless of which specific ERC20 token is being withdrawn:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This hardcoded limit is then passed directly into `create_transaction` and embedded into the signed EIP-1559 transaction submitted to Ethereum: [3](#0-2) 

The `create_transaction` function places this `gas_limit` directly into the `Eip1559TransactionRequest`: [4](#0-3) 

The documentation itself acknowledges this is a fixed value: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [5](#0-4) 

Similarly, the DID interface warns: *"The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail."* [6](#0-5) 

---

### Impact Explanation

Any ckERC20 token whose underlying ERC20 `transfer` function requires more than 65,000 gas will have its Ethereum withdrawal transaction fail with out-of-gas. This includes:

1. **Tokens with transfer hooks or callbacks** (e.g., ERC777-compatible tokens, fee-on-transfer tokens, tokens with blocklist checks like USDT variants).
2. **Proxy-pattern ERC20 tokens** where the `transfer` call dispatches through a proxy, consuming additional gas.
3. **Any ERC20 token after an Ethereum opcode repricing** (e.g., EIP-1884, EIP-2929 historically increased `SLOAD` costs, raising the gas cost of many ERC20 `transfer` implementations).

When the Ethereum transaction fails due to out-of-gas, the user's ckERC20 tokens are reimbursed, but the ckETH fee burned to pay for the transaction is **not reimbursed** (the documentation explicitly states "Overcharged transaction fees are not reimbursed"). [7](#0-6) 

This results in a permanent, irreversible loss of ckETH for any user attempting to withdraw a ckERC20 token that exceeds the hardcoded gas limit.

---

### Likelihood Explanation

The likelihood is **medium**. The `65_000` limit is generous for simple ERC20 tokens, but:

- The IC's chain-fusion system is designed to support arbitrary ERC20 tokens via the ledger suite orchestrator. New tokens can be added via governance proposals, and some of those tokens may have higher gas requirements.
- Ethereum has a history of opcode repricing (EIP-1884, EIP-2929) that has broken previously-safe gas assumptions.
- The entry path requires only an unprivileged user calling `withdraw_erc20` — no special privileges needed. [8](#0-7) 

---

### Recommendation

Replace the single hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` constant with a per-token configurable gas limit stored in the minter's state alongside each supported ckERC20 token. The `estimate_gas_limit` function should look up the per-token gas limit from state rather than returning a global constant. This mirrors the fix applied in the original Solidity report: replacing a hardcoded resource limit with a dynamic, configurable one.

---

### Proof of Concept

1. A new ckERC20 token is added to the minter (e.g., a token with transfer hooks requiring ~80,000 gas).
2. A user calls `withdraw_erc20` on the minter, burning their ckERC20 tokens and paying a ckETH fee.
3. The minter calls `estimate_gas_limit`, which returns `65_000` regardless of the token.
4. The minter constructs and signs an EIP-1559 transaction with `gas_limit = 65_000`.
5. The transaction is submitted to Ethereum and fails with out-of-gas (the ERC20 `transfer` needed ~80,000 gas).
6. The minter detects the failed transaction and reimburses the ckERC20 tokens, but the ckETH fee is permanently lost. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-263)
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L719-719)
```text
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L39-41)
```rust
use ic_cketh_minter::withdraw::{
    CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT, CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    process_reimbursement, process_retrieve_eth_requests,
```
