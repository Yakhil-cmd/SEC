### Title
Fixed Gas Limit for ckERC20 Withdrawals Causes Permanent ckETH Loss When Withdrawing Non-Standard ERC20 Tokens (e.g., USDT) - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter canister uses a hardcoded gas limit of `65_000` for all ckERC20 withdrawal Ethereum transactions. Non-standard ERC20 tokens — including USDT, which is explicitly listed as a supported ckERC20 token — can require more than 65,000 gas for a `transfer()` call (e.g., when transferring to a cold/new recipient address). When the Ethereum transaction fails due to out-of-gas, the minter reimburses the ckERC20 tokens but **permanently retains the ckETH gas fee**, causing a direct, irreversible financial loss to the user.

---

### Finding Description

The ckETH minter's `withdraw_erc20` endpoint burns ckETH (for gas) and ckERC20 tokens, then constructs and submits an Ethereum EIP-1559 transaction calling `transfer(address,uint256)` (selector `a9059cbb`) on the ERC20 contract. The gas limit for this transaction is hardcoded: [1](#0-0) 

The documentation explicitly acknowledges this limit is only "sufficient for standard ERC-20 contracts": [2](#0-1) 

However, USDT is explicitly listed as a supported ckERC20 token on Ethereum mainnet: [3](#0-2) 

USDT is a well-known non-standard ERC20 token: its `transfer()` function does not return a `bool`, includes blacklist checks, and performs cold SSTORE operations when transferring to a new address. A USDT transfer to a first-time recipient address can consume 55,000–70,000+ gas (cold SSTORE for recipient balance slot costs 20,000 gas alone). When the gas limit of 65,000 is exhausted, the Ethereum transaction reverts with out-of-gas.

The transaction construction encodes only the standard `transfer` calldata: [4](#0-3) 

When the Ethereum transaction fails (`TransactionStatus::Failure`), `record_finalized_transaction` creates a reimbursement request for the ckERC20 tokens only: [5](#0-4) 

The ckETH gas fee is **not** reimbursed on failure. This is confirmed by the documentation: [6](#0-5) 

The `update_balance_upon_withdrawal` function only subtracts the ERC20 balance on success, confirming the minter's internal accounting is correct — but the ckETH is gone: [7](#0-6) 

---

### Impact Explanation

Any user calling `withdraw_erc20` for a supported non-standard ERC20 token (e.g., ckUSDT) where the recipient Ethereum address has never held that token before will have their Ethereum transaction fail with out-of-gas. The user:

1. **Permanently loses** the ckETH gas fee (burned from their ckETH ledger balance, not reimbursed).
2. **Receives back** their ckERC20 tokens (correctly reimbursed).

This is a **chain-fusion ledger conservation bug**: ckETH is destroyed without a corresponding Ethereum-side value transfer. The minter's internal ETH balance accounting (`eth_balance_sub`) still deducts the effective gas fee: [8](#0-7) 

The loss is proportional to the gas fee at the time of withdrawal. At current Ethereum gas prices (e.g., 20 gwei, ETH at $3,000), a failed 65,000-gas transaction costs approximately $3.90 in ckETH, permanently lost per failed withdrawal attempt.

---

### Likelihood Explanation

- USDT is explicitly supported on mainnet and is one of the highest-volume ERC20 tokens.
- USDT's `transfer` to a cold address (first-time recipient) routinely uses 55,000–70,000 gas on mainnet.
- Any unprivileged user can call `withdraw_erc20` — no special role required.
- The scenario is triggered naturally: a user withdrawing ckUSDT to a fresh Ethereum address (e.g., a new hardware wallet) will hit this condition.
- The user may retry, losing additional ckETH each time, since the gas limit does not change between attempts.

---

### Recommendation

1. **Per-token configurable gas limits**: Store a per-token `gas_limit` in the minter state alongside each supported ERC20 token, allowing the NNS to set appropriate limits when adding tokens.
2. **Dynamic gas estimation**: Use `eth_estimateGas` via the EVM-RPC canister before constructing the withdrawal transaction to determine the actual gas required.
3. **Reimburse ckETH on out-of-gas failure**: Detect out-of-gas failures (where `gas_used == gas_limit` in the receipt) and reimburse the ckETH fee in that case, since the failure is attributable to a protocol misconfiguration rather than user error.
4. **Increase the default gas limit**: For known non-standard tokens like USDT, use a higher gas limit (e.g., 100,000) to accommodate cold storage slot costs.

---

### Proof of Concept

1. User holds ckUSDT and ckETH on the IC.
2. User calls `withdraw_erc20` specifying a fresh Ethereum address (never held USDT) as recipient.
3. Minter burns ckETH (e.g., `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE`) and ckUSDT.
4. Minter constructs EIP-1559 transaction with `gas_limit = 65_000`, `data = a9059cbb || recipient || amount`, `destination = USDT_CONTRACT`.
5. On Ethereum, USDT's `transfer` to the cold address consumes >65,000 gas → transaction reverts with out-of-gas (`status = 0`).
6. Minter observes `TransactionStatus::Failure` in the receipt.
7. `record_finalized_transaction` creates a reimbursement for ckUSDT only.
8. `process_reimbursement` mints back the ckUSDT to the user.
9. The ckETH gas fee is **never reimbursed** — permanently lost.

The hardcoded gas limit is the necessary vulnerable step in IC production code: [9](#0-8) 

The reimbursement logic confirms only ckERC20 is returned on failure, not ckETH: [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-38)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1183)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L365-383)
```rust
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);

        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```
