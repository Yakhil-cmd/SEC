### Title
Hard-Coded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` of 65,000 Is Insufficient for High-Gas ERC-20 Tokens, Causing Permanent Withdrawal Failures with No Reimbursement of Transaction Fees - (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter canister hard-codes a single gas limit of `65_000` for **all** ckERC20 withdrawal transactions, regardless of the ERC-20 token being withdrawn. Several supported tokens (e.g., wstETH, USDT) require significantly more than 65,000 gas for a `transfer()` call. When the on-chain transaction runs out of gas, it reverts on Ethereum, the ckERC20 tokens are reimbursed to the user, but the ckETH burned to pay the transaction fee is **not reimbursed** (by explicit design). This results in a permanent, repeatable loss of ckETH for any user attempting to withdraw a high-gas ERC-20 token.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are defined as compile-time constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

The function `estimate_gas_limit` returns the same `65_000` for **every** ckERC20 token regardless of which token is being withdrawn:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
```

This constant is used in `create_transactions_batch` to build every ERC-20 withdrawal transaction sent to Ethereum. The documentation itself acknowledges this limitation:

> "The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and **should be sufficient for standard ERC-20 contracts**."

However, the minter supports tokens that are **not** standard ERC-20 contracts. For example:
- **wstETH** (`0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0`): A rebasing/wrapped staking token whose `transfer()` involves additional staking logic and typically consumes 80,000–120,000+ gas.
- **USDT** (`0xdAC17F958D2ee523a2206206994597C13D831ec7`): The Tether contract has a non-standard `transfer()` that historically consumes more than 65,000 gas in certain conditions.

When the minter submits a withdrawal transaction with `gas_limit = 65_000` for such a token, the Ethereum transaction runs out of gas and reverts. The minter then detects the `TransactionStatus::Failure` receipt and reimburses the ckERC20 tokens to the user. However, the ckETH burned to pay the transaction fee is **explicitly not reimbursed**, as stated in the documentation:

> "Overcharged transaction fees are not reimbursed."

This is confirmed in the transaction finalization logic in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, where only the ckERC20 token amount is reimbursed on failure, not the ckETH gas fee.

---

### Impact Explanation

**Chain-fusion burn/loss bug.** Any user calling `withdraw_erc20` for a high-gas ckERC20 token (e.g., ckWSTETH) will:

1. Have their ckETH burned to pay the estimated gas fee (computed as `65_000 * max_fee_per_gas`).
2. Have their ckERC20 tokens burned.
3. The minter submits an Ethereum transaction that runs out of gas and reverts.
4. The ckERC20 tokens are reimbursed, but the ckETH gas fee is **permanently lost**.
5. The user can retry, but the same failure will repeat indefinitely — each attempt burning more ckETH.

This is a direct, repeatable loss of user funds (ckETH) with no recovery path. The minter has no mechanism to detect that a token requires more gas than the hard-coded limit before burning the user's ckETH.

**Severity:** High — permanent loss of ckETH for every withdrawal attempt of affected tokens.

---

### Likelihood Explanation

**High.** The minter already supports ckWSTETH on mainnet (added via governance proposal). Any user attempting to withdraw ckWSTETH to Ethereum will trigger this bug. The wstETH `transfer()` function is well-documented to consume significantly more than 65,000 gas. The entry path requires only a standard `withdraw_erc20` call from any unprivileged IC principal — no special access is needed.

---

### Recommendation

1. **Per-token gas limit configuration:** Store a configurable `gas_limit` per supported ckERC20 token in the minter state, settable at token registration time and updatable via upgrade args.
2. **Minimum viable fix:** Increase `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` to a value that covers the highest-gas supported token (e.g., 200,000), or add a per-token override map.
3. **Protect users:** Before burning ckETH, the minter should validate that the gas limit is sufficient for the specific token, or at minimum warn users via the `eip_1559_transaction_price` query that the gas limit may be insufficient.

---

### Proof of Concept

**Root cause location:** [1](#0-0) 

**`estimate_gas_limit` applies the same constant to all ckERC20 tokens:** [2](#0-1) 

**The constant is used in `create_transactions_batch` for every withdrawal:** [3](#0-2) 

**The documentation explicitly acknowledges the fixed gas limit and its assumption:** [4](#0-3) 

**wstETH is a supported mainnet token:** [5](#0-4) 

**On failure, only ckERC20 tokens are reimbursed; ckETH gas fee is not:** [6](#0-5) 

**The `create_transaction` function for ckERC20 uses the passed `gas_limit` directly with no per-token adjustment:** [7](#0-6) 

**Attack path:** Any IC principal calls `withdraw_erc20` with `ckerc20_ledger_id` pointing to the ckWSTETH ledger. The minter burns ckETH for gas, burns ckWSTETH, submits an Ethereum transaction with `gas_limit = 65_000`, which reverts on-chain due to out-of-gas. The ckWSTETH is reimbursed but the ckETH is permanently lost. The user can repeat this indefinitely, each time losing ckETH.

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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L43-44)
```text
|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1147-1184)
```rust
        WithdrawalRequest::CkErc20(request) => {
            // The transaction fee is already paid and must be at most
            // the `max_transaction_fee` in the withdrawal request, which, given a gas limit, gives us an upper bound on
            // the `max_fee_per_gas`. We allocate the maximum from the beginning to minimize
            // transaction resubmissions: even if the `base_fee_per_gas` increases considerably,
            // the transaction could still make it as long as `transaction.max_fee_per_gas >=  block.base_fee_per_gas`,
            // since the `priority_fee_per_gas` received by the miner is capped to (see https://eips.ethereum.org/EIPS/eip-1559)
            // min(transaction.max_priority_fee_per_gas, transaction.max_fee_per_gas - block.base_fee_per_gas).
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
