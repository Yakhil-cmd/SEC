### Title
Hardcoded `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 21_000` Causes Permanent Gas Fee Loss When Withdrawing to Smart Contract Addresses - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes a gas limit of 21,000 for all ETH withdrawal transactions. This is the exact cost of a plain EOA-to-EOA ETH transfer on Ethereum. Any smart contract recipient (multi-signature wallet, DeFi protocol, proxy contract) requires more than 21,000 gas to process an incoming ETH transfer, causing the Ethereum transaction to fail. When this happens, the minter reimburses only the ETH `amount` field of the failed transaction — not the full burned ckETH — so the user permanently loses the `max_transaction_fee` portion of their withdrawal.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, the gas limit for all ckETH withdrawal transactions is set to a compile-time constant:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns this constant for every ckETH withdrawal:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        ...
    }
}
``` [2](#0-1) 

`create_transaction` then embeds this limit directly into the EIP-1559 transaction sent to Ethereum:

```rust
gas_limit: transaction_price.gas_limit,
``` [3](#0-2) 

When the Ethereum transaction fails (status = `Failure`), `record_finalized_transaction` schedules a reimbursement of only `finalized_tx.transaction_amount()` — the `amount` field of the transaction, which equals `withdrawal_amount - max_transaction_fee`:

```rust
reimbursed_amount: finalized_tx.transaction_amount().change_units(),
``` [4](#0-3) 

The `max_transaction_fee` (= `gas_limit × max_fee_per_gas` = `21_000 × max_fee_per_gas`) is never reimbursed. The DID file acknowledges this behavior with a comment but does not enforce any restriction at the protocol level:

```
// IMPORTANT: The current gas limit is set to 21,000 for a transaction
// so withdrawals to smart contract addresses will likely fail.
withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
``` [5](#0-4) 

The `withdraw_eth` handler in `main.rs` performs no check that the recipient is an EOA; it only validates the address format and blocklist status: [6](#0-5) 

---

### Impact Explanation

A user who calls `withdraw_eth` specifying a smart contract address (e.g., a Gnosis Safe multi-sig, a DeFi vault, a proxy) as the recipient will:

1. Have their ckETH burned on the IC ledger (irreversible at this point).
2. Receive an Ethereum transaction with `gas_limit = 21_000` that fails on-chain because the smart contract's receive/fallback function requires more gas.
3. Be reimbursed only `withdrawal_amount - max_transaction_fee` in ckETH — permanently losing the gas fee.

From the integration test `should_reimburse`, the confirmed loss per failed withdrawal is on the order of `693_077_873_418_000` wei (~0.000693 ETH) at typical gas prices: [7](#0-6) 

This is a **ledger conservation / chain-fusion burn-without-delivery bug**: ckETH is burned but ETH is not delivered, and the gas fee is permanently destroyed rather than returned.

---

### Likelihood Explanation

The entry point is the public `withdraw_eth` update call, reachable by any unprivileged IC principal. Multi-signature wallets (Gnosis Safe, etc.) and DeFi protocols are extremely common Ethereum recipients. A user who holds ckETH and wants to withdraw to their team's multi-sig will trigger this path without any protocol-level warning or rejection. The DID comment is the only guard, and it is not surfaced to end users through wallets or frontends that consume the Candid interface.

---

### Recommendation

1. **Validate recipient type before accepting the withdrawal**: Query the Ethereum node (via the EVM RPC canister) to check whether the destination address has code (`eth_getCode`). Reject the withdrawal with a clear error if the recipient is a contract and the gas limit is insufficient.
2. **Allow a user-specified gas limit**: Extend `WithdrawalArg` with an optional `gas_limit` field, capped at a protocol maximum, so users withdrawing to smart contracts can supply a sufficient limit.
3. **Reimburse the full `withdrawal_amount` on failure**: If the transaction fails, reimburse `withdrawal_amount` (not `transaction_amount`) so users are not penalized for a protocol-imposed limitation.

---

### Proof of Concept

1. User holds 0.1 ckETH and calls `withdraw_eth` with `recipient = "0x<GnosisSafeAddress>"` and `amount = 100_000_000_000_000_000` (0.1 ETH).
2. Minter burns 0.1 ckETH on the IC ledger.
3. Minter constructs an EIP-1559 transaction: `gas_limit = 21_000`, `amount = 0.1 ETH - max_tx_fee`.
4. Gnosis Safe's `receive()` fallback requires ~30,000–50,000 gas; the transaction reverts with "out of gas."
5. Minter detects `TransactionStatus::Failure` in `record_finalized_transaction`.
6. Reimbursement is scheduled for `finalized_tx.transaction_amount()` = `0.1 ETH - max_tx_fee`, not `0.1 ETH`.
7. User receives back `~0.0993 ckETH`; `~0.0007 ckETH` (~$2–$3 at current prices) is permanently lost per withdrawal attempt. [8](#0-7) [1](#0-0)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L680-748)
```rust
    pub fn record_finalized_transaction(
        &mut self,
        ledger_burn_index: LedgerBurnIndex,
        receipt: TransactionReceipt,
    ) {
        let sent_tx = self
            .sent_tx
            .get_alt(&ledger_burn_index)
            .expect("BUG: missing sent transactions")
            .iter()
            .find(|sent_tx| sent_tx.as_ref().hash() == receipt.transaction_hash)
            .expect("ERROR: no transaction matching receipt");
        let finalized_tx = sent_tx
            .as_ref()
            .clone()
            .try_finalize(receipt.clone())
            .expect("ERROR: invalid transaction receipt");

        let nonce = sent_tx.as_ref().nonce();
        {
            self.sent_tx.remove_entry(&nonce);
            Self::cleanup_failed_resubmitted_transactions(&mut self.created_tx, &nonce);
        }
        assert_eq!(
            self.finalized_tx
                .try_insert(nonce, ledger_burn_index, finalized_tx.clone()),
            Ok(())
        );

        assert!(
            self.maybe_reimburse.remove(&ledger_burn_index),
            "failed to remove entry from maybe_reimburse with block index: {ledger_burn_index}",
        );

        let request = self.processed_withdrawal_requests
            .get(&ledger_burn_index)
            .expect("failed to find entry from processed_withdrawal_requests with block index: {ledger_burn_index}");
        let index = ReimbursementIndex::from(request);
        match &request {
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
            }
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
        }
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L719-720)
```text
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L280-296)
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
