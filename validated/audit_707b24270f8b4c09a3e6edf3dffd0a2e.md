### Title
Fixed 21,000 Gas Limit Causes ckETH Withdrawals to Smart Contract Addresses to Permanently Fail — (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes a gas limit of `21_000` for all ckETH ETH-withdrawal transactions. This is the exact analog of Solidity's `transfer()` with its 2,300 gas stipend: any Ethereum smart contract address that requires more than 21,000 gas to receive ETH (e.g., Gnosis Safe multisigs, DeFi vaults, proxy wallets) will have its withdrawal transaction fail on-chain. The user's ckETH is burned first, the Ethereum transaction reverts, and the user loses the full estimated transaction fee even though no ETH was delivered.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, the gas limit for ckETH withdrawals is a compile-time constant:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns this constant for every ckETH withdrawal, regardless of whether the destination is an EOA or a smart contract:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

The `validate_address_as_destination` function, called in `withdraw_eth` before burning, only checks that the address is syntactically valid, non-zero, and not on the blocklist. It performs no check for whether the address is a smart contract:

```rust
pub fn validate_address_as_destination(address: &str) -> Result<Address, AddressValidationError> {
    let address = Address::from_str(address)...?;
    if address == Address::ZERO { return Err(...); }
    if crate::blocklist::is_blocked(&address) { return Err(...); }
    Ok(address)
}
``` [3](#0-2) 

The `withdraw_eth` update method burns ckETH from the caller's ledger account **before** the Ethereum transaction is sent: [4](#0-3) 

The `create_transaction` function then builds an EIP-1559 transaction with `gas_limit = 21_000` and `data: Vec::new()` (no calldata), which is only sufficient for a plain ETH transfer to an EOA: [5](#0-4) 

The DID interface itself acknowledges this limitation in a comment:

> "IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail." [6](#0-5) 

---

### Impact Explanation

When a user calls `withdraw_eth` specifying a smart contract address (e.g., a Gnosis Safe multisig, a DeFi vault, or any proxy wallet) as the recipient:

1. ckETH is burned from the user's ledger account (irreversible at this step).
2. The minter creates and signs an Ethereum transaction with `gas_limit = 21_000`.
3. The transaction is submitted to Ethereum. Smart contract `receive()` or `fallback()` functions that perform any non-trivial logic (storage writes, event emissions, proxy delegation) consume more than 21,000 gas and cause the transaction to revert with out-of-gas.
4. The minter detects the failure and reimburses the user via `process_reimbursement`, minting back `withdrawal_amount - max_tx_fee_estimate`.
5. The user permanently loses the full estimated Ethereum transaction fee (`max_tx_fee_estimate = gas_limit × max_fee_per_gas = 21,000 × current_gas_price`), which at typical mainnet gas prices can be several dollars to tens of dollars, despite receiving zero ETH at the destination.

This is a **ledger conservation bug / chain-fusion burn-without-delivery bug**: ckETH is burned and a fee is consumed, but the ETH is never delivered to the intended smart contract recipient. [7](#0-6) 

---

### Likelihood Explanation

**High.** A large fraction of Ethereum users hold ETH in smart contract wallets (Gnosis Safe is the most popular multisig on Ethereum mainnet, used by DAOs, protocols, and institutional holders). Any ckETH holder who attempts to withdraw to their multisig or DeFi contract will trigger this failure. The minter accepts the withdrawal request without any on-chain check or user-facing warning at call time — the only warning is buried in the DID documentation comment. The entry path requires no privilege: any unprivileged IC principal with a ckETH balance can trigger this by calling `withdraw_eth` with a smart contract address.

---

### Recommendation

1. **Increase the gas limit** for ckETH withdrawals to a value sufficient for common smart contract wallets (e.g., 100,000–200,000 gas), similar to how `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is already set to 65,000.
2. **Allow callers to specify a custom gas limit** (bounded by a protocol maximum) in `WithdrawalArg`, so users withdrawing to smart contracts can supply an appropriate value.
3. **Reject withdrawals to known smart contract addresses** at the `validate_address_as_destination` step by querying the Ethereum node for the address's code via `eth_getCode`, or at minimum surface a clear error to the caller rather than silently burning ckETH and refunding minus fees.

---

### Proof of Concept

1. User holds 1 ckETH and calls:
   ```
   withdraw_eth(record { amount = 1_000_000_000_000_000_000; recipient = "0xGnosisSafeAddress" })
   ```
2. `validate_address_as_destination` passes (address is valid, non-zero, not blocked).
3. `burn_from` burns 1 ckETH from the user's ledger account.
4. `estimate_gas_limit` returns `GasAmount::new(21_000)`.
5. `create_transaction` builds an EIP-1559 tx: `gas_limit=21_000, data=[]`.
6. Transaction is signed via threshold ECDSA and submitted to Ethereum.
7. Gnosis Safe's `receive()` proxy logic consumes >21,000 gas → transaction reverts out-of-gas.
8. `process_reimbursement` mints back `1 ckETH - max_tx_fee_estimate` to the user.
9. User loses `max_tx_fee_estimate` (e.g., ~$5–$50 at typical gas prices) and receives 0 ETH. [1](#0-0) [2](#0-1) [3](#0-2) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-147)
```rust
pub async fn process_reimbursement() {
    let _guard = match TimerGuard::new(TaskType::Reimbursement) {
        Ok(guard) => guard,
        Err(e) => {
            log!(DEBUG, "Failed retrieving reimbursement guard: {e:?}",);
            return;
        }
    };

    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
    if reimbursements.is_empty() {
        return;
    }

    let mut error_count = 0;

    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
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
        let reimbursed = Reimbursed {
            burn_in_block: reimbursement_request.ledger_burn_index,
            reimbursed_in_block: LedgerMintIndex::new(block_index),
            reimbursed_amount: reimbursement_request.reimbursed_amount,
            transaction_hash: reimbursement_request.transaction_hash,
        };
        let event = match index {
            ReimbursementIndex::CkEth {
                ledger_burn_index: _,
            } => EventType::ReimbursedEthWithdrawal(reimbursed),
            ReimbursementIndex::CkErc20 {
                cketh_ledger_burn_index,
                ledger_id,
                ckerc20_ledger_burn_index: _,
            } => EventType::ReimbursedErc20Withdrawal {
                cketh_ledger_burn_index,
                ckerc20_ledger_id: ledger_id,
                reimbursed,
            },
        };
        mutate_state(|s| process_event(s, event));
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
    }
    if error_count > 0 {
        log!(
            INFO,
            "[process_reimbursement] Failed to reimburse {error_count} users, retrying later."
        );
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

**File:** rs/ethereum/cketh/minter/src/address.rs (L47-56)
```rust
pub fn validate_address_as_destination(address: &str) -> Result<Address, AddressValidationError> {
    let address =
        Address::from_str(address).map_err(|e| AddressValidationError::Invalid { error: e })?;
    if address == Address::ZERO {
        return Err(AddressValidationError::NotSupported(address));
    }
    if crate::blocklist::is_blocked(&address) {
        return Err(AddressValidationError::Blocked(address));
    }
    Ok(address)
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L280-340)
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```
