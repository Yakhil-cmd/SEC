### Title
Permanent Loss of ckETH Gas Fees When `withdraw_eth` Targets a Smart Contract Address — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The ckETH minter's `withdraw_eth` endpoint accepts any non-blocked Ethereum address as a withdrawal destination, including smart contract addresses. The minter hard-codes a gas limit of `21,000` for all ckETH withdrawal transactions — sufficient only for plain ETH transfers to EOAs. When the destination is a smart contract that requires more than 21,000 gas to receive ETH, the Ethereum transaction predictably fails. Upon failure, the minter reimburses only the ETH value portion of the withdrawal; the ckETH burned to cover the gas fee is permanently lost. No enforcement prevents users from targeting smart contract addresses, and the only warning exists as a comment in the Candid interface file.

---

### Finding Description

**Root cause — hardcoded gas limit:** [1](#0-0) 

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is fixed at `21_000`. This is the standard gas cost for a plain ETH transfer to an EOA. Any smart contract with a non-trivial `receive` or `fallback` function will consume more gas and cause the transaction to revert.

**Root cause — no enforcement at the API layer:** [2](#0-1) 

`validate_address_as_destination` only rejects invalid or blocklisted addresses. It does not reject smart contract addresses. The only mention of the limitation is a comment in the DID file: [3](#0-2) 

**Root cause — gas fee not reimbursed on failure:**

When the Ethereum transaction is finalized with `TransactionStatus::Failure`, `record_finalized_transaction` creates a reimbursement request for only `finalized_tx.transaction_amount()` — the ETH value sent in the transaction — not the gas fee: [4](#0-3) 

The gas fee (`withdrawal_amount - transaction_amount`) is permanently consumed by the failed Ethereum transaction and is never returned to the user.

**Reimbursement flow confirms the loss:** [5](#0-4) 

The `process_reimbursement` function mints back only `reimbursement_request.reimbursed_amount`, which for a ckETH failure equals `finalized_tx.transaction_amount()` — the ETH value, not the full burned amount.

---

### Impact Explanation

A user who calls `withdraw_eth` targeting a smart contract address (e.g., a DeFi protocol, multisig wallet, or any contract whose `receive` function costs more than 21,000 gas) will:

1. Have their ckETH burned in full (`withdrawal_amount`).
2. Receive back only `withdrawal_amount - max_tx_fee_estimate` in ckETH after the Ethereum transaction fails.
3. Permanently lose `max_tx_fee_estimate` worth of ckETH (the gas fee estimate), which at current Ethereum gas prices can be tens of dollars per transaction.

This is a direct ledger conservation bug: ckETH is burned in excess of what is actually consumed by the protocol, with no recovery path.

---

### Likelihood Explanation

The vulnerability is reachable by any unprivileged IC principal calling `withdraw_eth`. The warning exists only as a comment in the `.did` file — not as an on-chain error or rejection. Developers building IC canisters that interact with the ckETH minter (e.g., to withdraw ETH to a smart contract treasury or multisig) are likely to encounter this silently. The pattern of withdrawing ckETH to a smart contract address is a natural use case that is not blocked.

---

### Recommendation

1. **Enforce at the API layer**: Reject withdrawal requests where the destination is a known smart contract address, or at minimum emit a structured error (not just a comment) when the destination is likely a contract.
2. **Reimburse the full burned amount on failure**: When the Ethereum transaction fails, reimburse `withdrawal_amount` minus only the actual gas consumed (`effective_tx_fee`), not `withdrawal_amount - max_tx_fee_estimate`. The current logic already computes `effective_tx_fee` from the receipt.
3. **Alternatively, increase the gas limit**: Use a higher gas limit (e.g., 65,000 as used for ckERC20) to accommodate smart contract recipients, accepting the higher fee cost.

---

### Proof of Concept

1. An IC canister (smart contract) holds ckETH and calls `withdraw_eth` with its own Ethereum address (a contract address) as the recipient.
2. The minter burns the full `withdrawal_amount` of ckETH from the canister's account.
3. The minter constructs an EIP-1559 transaction with `gas_limit = 21_000` to the contract address. [6](#0-5) 

4. The Ethereum transaction is mined but fails (status = `Failure`) because the destination contract's `receive` function requires more than 21,000 gas.
5. `record_finalized_transaction` creates a reimbursement for `finalized_tx.transaction_amount()` only: [7](#0-6) 

6. `process_reimbursement` mints back only the ETH value, not the gas fee. The canister permanently loses `max_tx_fee_estimate` ckETH (e.g., ~693,077,873,418,000 wei ≈ 0.00069 ETH per the test fixture): [8](#0-7) 

The gas fee is gone with no recourse, mirroring the M-07 pattern where funds are irrecoverably lost when a transfer is sent to a contract that cannot handle the token type.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L82-117)
```rust
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
            Ok(Ok(block_index)) => block_index
                .0
                .to_u64()
                .expect("block index should fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "[process_reimbursement] Failed to mint ckETH {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "[process_reimbursement] Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L280-287)
```rust
    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
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
