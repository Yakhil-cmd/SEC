### Title
Hardcoded 21,000 Gas Limit in `withdraw_eth` Causes ETH Withdrawal Failure and Fee Loss When Recipient Is a Smart Contract - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

---

### Summary

The ckETH minter's `withdraw_eth` endpoint uses a hardcoded gas limit of `21_000` for all ETH withdrawal transactions, regardless of whether the recipient Ethereum address is an EOA or a smart contract. A gas limit of 21,000 is the exact base cost of a plain ETH transfer to an EOA — it leaves zero gas for any smart contract `receive()` or `fallback()` logic. Any user who withdraws ckETH to a smart contract address (e.g., a multisig, a DeFi vault, a proxy wallet) will have their Ethereum transaction fail on-chain. The ckETH burn on the IC side is irreversible; the user loses at minimum the full `max_tx_fee_estimate` deducted at withdrawal time, and if the reimbursement path is quarantined, the entire withdrawal amount is permanently unrecoverable.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limit constants are defined:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` for all ckETH withdrawal requests:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This gas limit is applied in `create_transactions_batch` for every ckETH withdrawal: [3](#0-2) 

The `withdraw_eth` endpoint in `rs/ethereum/cketh/minter/src/main.rs` accepts any valid, non-blocked Ethereum address as the recipient — including smart contract addresses — without any check or warning at the call site: [4](#0-3) 

The only disclosure of this limitation is a comment in the Candid interface file:

> "IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail." [5](#0-4) 

This comment is not enforced at the protocol level. The `withdraw_eth` call proceeds, burns ckETH from the user's ledger account (irreversible), and then submits an Ethereum transaction with `gas_limit = 21_000`. Since 21,000 gas is the exact base cost of a plain ETH transfer, there is literally **zero gas remaining** for any smart contract `receive()` or `fallback()` execution. The Ethereum transaction reverts with out-of-gas.

The ckETH withdrawal flow burns tokens **before** the Ethereum transaction is sent: [6](#0-5) 

The reimbursement path (`process_reimbursement`) can mint ckETH back to the user if the Ethereum transaction fails, but:
1. The `max_tx_fee_estimate` deducted at withdrawal time is **never reimbursed** (per protocol design).
2. If the reimbursement itself panics, the request is moved to `QuarantinedReimbursement` and is not automatically retried: [7](#0-6) 

---

### Impact Explanation

Any user who calls `withdraw_eth` with a smart contract address as the recipient (e.g., a Gnosis Safe multisig, a DeFi protocol vault, a proxy wallet, or any contract with a non-trivial `receive()` function) will:

1. Have their ckETH burned on the IC ledger (irreversible).
2. Have the Ethereum transaction fail on-chain due to out-of-gas.
3. Lose the full `max_tx_fee_estimate` (which can be substantial during high-fee periods) with no reimbursement.
4. In the worst case (quarantined reimbursement), lose the entire withdrawal amount permanently.

This is a **ledger conservation bug** and **chain-fusion mint/burn bug**: ckETH is destroyed on the IC side but the corresponding ETH is never delivered to the intended recipient.

---

### Likelihood Explanation

Smart contract addresses are extremely common withdrawal destinations in DeFi. Multisig wallets (Gnosis Safe), protocol treasuries, yield aggregators, and proxy wallets are all smart contracts. A user who copies their wallet address from a dApp UI may not know or care whether it is an EOA or a contract. The only warning is a comment in the `.did` file — not a runtime error, not a UI warning, not a rejected transaction. The likelihood of accidental fund loss is **high** for any user interacting with the ckETH minter from a smart-contract-based wallet.

---

### Recommendation

1. **Enforce a minimum gas limit above 21,000** for ckETH withdrawals to allow smart contract recipients to execute their `receive()` logic (e.g., use 65,000 as already done for ckERC20, or make it configurable).
2. **Alternatively**, reject `withdraw_eth` calls where the recipient is a known smart contract address (by checking Ethereum contract code via `eth_getCode` through the EVM RPC canister before accepting the withdrawal).
3. **At minimum**, return a `WithdrawalError` variant (e.g., `RecipientMayBeSmartContract`) rather than silently proceeding when the destination is a contract, so users are warned before ckETH is burned.
4. Ensure the reimbursement path for failed ckETH transactions is robust and that quarantined reimbursements are surfaced and retried.

---

### Proof of Concept

1. User holds ckETH and calls `withdraw_eth` on the minter with `recipient = "0x<GnosisSafeAddress>"` (a smart contract multisig).
2. The minter validates the address (passes — it is a valid, non-blocked address), burns the ckETH from the user's ledger account.
3. `create_transactions_batch` calls `estimate_gas_limit`, which returns `GasAmount::new(21_000)`.
4. The minter constructs and signs an EIP-1559 transaction with `gas_limit = 21_000` and `value = withdraw_amount - max_tx_fee_estimate`.
5. The transaction is submitted to Ethereum. The Gnosis Safe's `receive()` function requires >21,000 gas; the transaction reverts with out-of-gas.
6. The minter reads the failed receipt in `finalize_transactions_batch`. The user's ckETH has already been burned. The `max_tx_fee_estimate` (e.g., ~0.005 ETH at current prices) is permanently lost. The remaining principal may be reimbursed as ckETH only if the reimbursement path succeeds without panicking.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L68-72)
```rust
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L257-264)
```rust
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```
