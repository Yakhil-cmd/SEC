### Title
Fixed 21,000 Gas Limit in ckETH Minter `withdraw_eth` Guarantees Transaction Failure and Permanent Gas-Fee Loss When Recipient Is a Smart Contract - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `withdraw_eth` endpoint uses a hardcoded gas limit of `21,000` for all ETH withdrawal transactions. This is identical in spirit to Solidity's deprecated `transfer()` pattern: it is sufficient only for EOA (externally owned account) recipients. Any withdrawal targeting an Ethereum smart contract address will fail on-chain because the contract's `receive()` or fallback function requires more than 21,000 gas. The ckETH burn on the IC ledger is irreversible at the point of the on-chain failure; the user is eventually reimbursed minus the gas fee actually consumed by the failed transaction, resulting in a guaranteed, quantifiable ckETH loss for every such withdrawal.

---

### Finding Description

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is a compile-time constant set to `21_000`: [1](#0-0) 

This constant is used unconditionally for every ckETH withdrawal in `estimate_gas_limit`: [2](#0-1) 

The `withdraw_eth` handler burns the caller's ckETH on the IC ledger first, then enqueues the withdrawal request: [3](#0-2) 

The only pre-flight validation on the recipient address is a blocklist check (`validate_address_as_destination`); there is no check that the recipient is an EOA rather than a smart contract: [4](#0-3) 

When the Ethereum transaction is constructed, the gas limit is taken directly from `estimate_gas_limit`, which always returns `21_000` for ckETH withdrawals: [5](#0-4) 

If the recipient is a smart contract whose `receive()` or fallback function consumes more than 21,000 gas, the Ethereum transaction is mined but reverts with `TransactionStatus::Failure`. The minter then creates a `ReimbursementRequest` for `withdrawal_amount - effective_transaction_fee`: [6](#0-5) 

The `process_reimbursement` timer task eventually mints back the reduced amount to the user: [7](#0-6) 

The DID file itself acknowledges this limitation inline but does not enforce it at the API level: [8](#0-7) 

---

### Impact Explanation

Every call to `withdraw_eth` targeting a smart contract address results in:

1. **Irreversible ckETH burn** on the IC ledger at call time.
2. **On-chain transaction failure** — the ETH is never delivered to the intended recipient.
3. **Permanent gas-fee loss** — the user is reimbursed `withdrawal_amount − (gas_used × effective_gas_price)`. At 21,000 gas and typical Ethereum gas prices (1–50 gwei), this is 0.000021–0.00105 ETH per failed withdrawal. At elevated gas prices (e.g., 100+ gwei during congestion), the loss per withdrawal exceeds 0.002 ETH (~$5+).
4. **Delayed fund recovery** — reimbursement is asynchronous and depends on the timer-driven `process_reimbursement` task completing successfully.

Additionally, if the reimbursement ledger call succeeds but the subsequent `.expect("block index should fit into u64")` panics (e.g., if the ledger ever returns a block index overflowing `u64`), the `scopeguard` fires `QuarantinedReimbursement`, permanently losing the entire remaining withdrawal amount: [9](#0-8) [10](#0-9) 

---

### Likelihood Explanation

The entry path requires only an unprivileged call to `withdraw_eth` with a smart contract address as `recipient`. No special role, key, or governance majority is needed. Smart contract wallets (e.g., Gnosis Safe, account-abstraction wallets) are increasingly common Ethereum recipients. Any user who attempts to withdraw ckETH to such an address will trigger the failure deterministically. The minter itself documents the issue but does not prevent it.

---

### Recommendation

1. **Enforce EOA-only recipients at the API level**: Before burning ckETH, query the Ethereum network (via the EVM-RPC canister) to verify that the recipient address has no deployed code (`eth_getCode` returning `0x`). Reject the withdrawal with a clear error if the recipient is a contract.
2. **Alternatively, raise the gas limit**: Allow a configurable or per-request gas limit for ckETH withdrawals so that smart contract recipients with reasonable `receive()` functions can be served. This mirrors the approach already taken for ckERC20 withdrawals (`CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`).
3. **Harden the reimbursement path**: Replace `.expect("block index should fit into u64")` with a graceful error path that defuses the guard rather than panicking, to prevent `QuarantinedReimbursement` from permanently locking user funds.

---

### Proof of Concept

1. User holds ckETH and calls:
   ```
   withdraw_eth(record { amount = 5_000_000_000_000_000; recipient = "0x<gnosis-safe-address>" })
   ```
2. The minter burns `5_000_000_000_000_000` wei of ckETH from the user's ledger account (irreversible).
3. The minter constructs an EIP-1559 transaction with `gas_limit = 21_000` to the Gnosis Safe address.
4. The Gnosis Safe's `receive()` function requires ~30,000 gas; the transaction reverts on Ethereum with `status = Failure`.
5. The minter records a `ReimbursementRequest` for `5_000_000_000_000_000 − (21_000 × effective_gas_price)`.
6. After the reimbursement timer fires, the user receives back approximately `5_000_000_000_000_000 − 90_000_000_000_000` wei (at 4.3 gwei effective gas price), permanently losing ~0.00009 ETH in gas fees with zero ETH delivered to the intended recipient. [1](#0-0) [11](#0-10) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-141)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-339)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1145)
```rust
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```
