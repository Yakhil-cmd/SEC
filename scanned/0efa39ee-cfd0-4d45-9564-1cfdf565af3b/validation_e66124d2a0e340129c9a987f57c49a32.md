### Title
Fixed Gas Limit of 21,000 in ckETH Minter Causes Withdrawals to Smart Contract Addresses to Fail with Permanent Fee Loss - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes a gas limit of `21_000` for all ETH withdrawal transactions. This is only sufficient for simple ETH transfers to Externally Owned Accounts (EOAs). When a user withdraws ckETH to a smart contract address (e.g., a multisig wallet, a DeFi protocol, or a smart contract wallet), the Ethereum transaction will fail due to insufficient gas. The user's ckETH is burned before the transaction is sent, and while the minter reimburses the withdrawal amount minus the effective transaction fee, the user permanently loses the gas fees paid for the failed transaction.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, the constant `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is hardcoded to `21_000` gas units: [1](#0-0) 

The `estimate_gas_limit` function always returns this fixed value for ckETH withdrawals, regardless of the recipient address type: [2](#0-1) 

The `withdraw_eth` endpoint in `rs/ethereum/cketh/minter/src/main.rs` accepts any non-blocked Ethereum address as a recipient — including smart contract addresses — and immediately burns the user's ckETH before the Ethereum transaction is even constructed: [3](#0-2) 

The DID interface itself acknowledges this limitation explicitly: [4](#0-3) 

The sequence of events when a user withdraws to a smart contract address:
1. User calls `withdraw_eth` with a smart contract address as recipient.
2. The minter burns the user's ckETH on the IC ledger (irreversible at this point).
3. The minter constructs an EIP-1559 transaction with `gas_limit = 21_000`.
4. The transaction is submitted to Ethereum via threshold ECDSA.
5. The transaction fails on Ethereum because the recipient smart contract's `receive()` or `fallback()` requires more than 21,000 gas.
6. The minter detects the failure and schedules a reimbursement of `withdrawal_amount - effective_fee_paid`.
7. The user permanently loses the effective transaction fee.

The reimbursement flow confirms that the user loses the gas cost of the failed transaction: [5](#0-4) 

---

### Impact Explanation

This is a **chain-fusion ledger conservation bug**. Users who withdraw ckETH to smart contract addresses (multisig wallets such as Gnosis Safe, DeFi protocol contracts, smart contract wallets like Argent) will:

1. Have their Ethereum transaction fail due to the hardcoded 21,000 gas limit being insufficient for any smart contract `receive()`/`fallback()` execution.
2. Permanently lose the effective transaction fee (`gas_used × effective_gas_price`). At current Ethereum gas prices this can be on the order of tens to hundreds of USD.
3. Receive only a partial reimbursement of their ckETH (the withdrawal amount minus the gas fees consumed by the failed transaction).

The ckETH is burned on the IC side before the Ethereum transaction is sent, so the burn is irreversible. The user cannot recover the gas fees lost to the failed transaction. This is a direct, quantifiable financial loss triggered by a design choice in the minter.

---

### Likelihood Explanation

- **Any unprivileged user** can trigger this by calling `withdraw_eth` with a smart contract address as the recipient. No special role or privilege is required.
- Smart contract wallets (Gnosis Safe, Argent, etc.) are increasingly common as recipients of ETH transfers. Many institutional and DeFi users exclusively use multisig addresses.
- DeFi protocols and DAOs routinely use smart contract treasury addresses.
- The minter performs no on-chain check to distinguish EOAs from smart contracts before burning the user's ckETH.
- While the DID file warns about this, the warning is easily missed by users interacting via frontends or third-party integrations that do not surface the DID comment.

---

### Recommendation

1. **Validate before burning**: Before burning the user's ckETH, query the Ethereum JSON-RPC provider to check whether the recipient address has contract code (`eth_getCode`). If it does, reject the withdrawal with a clear error, or require the user to explicitly acknowledge the risk and provide a higher gas limit.
2. **Allow user-specified gas limit**: Extend the `WithdrawalArg` type to accept an optional `gas_limit` field, allowing users to specify a higher gas limit when withdrawing to smart contract addresses.
3. **Increase the default gas limit**: Use a higher default gas limit (e.g., 100,000) that is sufficient for most smart contract `receive()` implementations, at the cost of a slightly higher fee estimate for EOA withdrawals.
4. **Reject smart contract destinations at the API level**: If the intent is to only support EOA recipients, enforce this at the `withdraw_eth` endpoint before any ckETH is burned, so users receive a clean error rather than a partial reimbursement after a failed Ethereum transaction.

---

### Proof of Concept

**Attacker-controlled entry path**: Any IC principal holding ckETH.

**Steps**:
1. User holds ckETH and calls `icrc2_approve` on the ckETH ledger to allow the minter to burn their tokens.
2. User calls `withdraw_eth` on the ckETH minter canister with a Gnosis Safe multisig address (or any smart contract address) as the `recipient`.
3. The minter burns the user's ckETH on the IC ledger — this is irreversible.
4. The minter constructs an EIP-1559 transaction with `gas_limit = CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 21_000`.
5. The transaction is signed via threshold ECDSA and submitted to Ethereum.
6. The Gnosis Safe contract's `receive()` function requires more than 21,000 gas (Gnosis Safe's fallback handler alone costs ~6,000 gas on top of the base 21,000, and the Safe's internal accounting requires additional gas). The transaction runs out of gas and reverts.
7. The minter observes the `TransactionStatus::Failure` receipt and schedules a reimbursement of `withdrawal_amount - effective_fee_paid`.
8. The user receives back their ckETH minus the gas fees paid for the failed transaction — a permanent, unrecoverable loss.

**Root cause line**: [1](#0-0) 

This is a direct analog to M-4: just as Solidity's `transfer()` forwards a fixed 2,300 gas causing failures for smart contract recipients, the ckETH minter's hardcoded `21_000` gas limit causes the same class of failure for smart contract recipients on Ethereum, with the added severity that the user's ckETH is burned on the IC side before the failure is discovered.

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L280-339)
```rust
    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1723-1737)
```rust
            let effective_fee_paid = finalized_transaction.effective_transaction_fee();
            assert_eq!(
                reimbursement_request,
                &ReimbursementRequest {
                    transaction_hash: Some(receipt.transaction_hash),
                    ledger_burn_index: cketh_ledger_burn_index,
                    to: withdrawal_request.from,
                    to_subaccount: withdrawal_request.from_subaccount,
                    reimbursed_amount: withdrawal_request
                        .withdrawal_amount
                        .checked_sub(effective_fee_paid)
                        .unwrap()
                        .change_units()
                }
            );
```
