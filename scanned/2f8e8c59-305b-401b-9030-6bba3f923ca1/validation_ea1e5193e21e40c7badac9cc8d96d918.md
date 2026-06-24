### Title
Fixed Gas Limit of 21,000 Causes ckETH Withdrawals to Smart Contract Addresses to Fail, Resulting in User Fund Loss - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

---

### Summary

The ckETH minter hardcodes `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` to exactly `21_000` gas units for all ETH withdrawal transactions. This is the bare minimum required for a plain ETH transfer to an EOA (Externally Owned Account). Any withdrawal targeting a smart contract address (multisig wallets, DeFi protocols, smart contract wallets like Gnosis Safe) will fail on Ethereum because smart contract `receive()` or `fallback()` execution requires more than 21,000 gas. The user's ckETH is burned upfront; when the Ethereum transaction fails, the minter reimburses only `withdrawal_amount - actual_tx_fee`, meaning the user permanently loses the gas fee paid for the failed transaction.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are defined as compile-time constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

The function `estimate_gas_limit` unconditionally returns `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` for all `CkEth` withdrawal requests, regardless of whether the destination is an EOA or a smart contract:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
```

This gas limit is then passed directly into `create_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, which constructs the EIP-1559 transaction with `gas_limit: transaction_price.gas_limit` (derived from the fixed 21,000 value).

The `withdraw_eth` endpoint in `rs/ethereum/cketh/minter/src/main.rs` accepts any non-blocked Ethereum address as a recipient. The `validate_address_as_destination` call only checks address validity and blocklist membership — it does **not** check whether the recipient is a smart contract. The DID interface itself acknowledges this in a comment:

> `// IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.`

The withdrawal flow is:
1. User calls `withdraw_eth(amount, smart_contract_address)`.
2. Minter calls `icrc2_transfer_from` to burn `amount` of ckETH from the user's account.
3. Minter constructs an Ethereum transaction with `gas_limit = 21_000` and `value = amount - max_tx_fee_estimate`.
4. The Ethereum transaction is broadcast. The smart contract's `receive()` or `fallback()` requires more than 21,000 gas → transaction reverts with out-of-gas.
5. Minter detects the failed transaction receipt and schedules reimbursement of `withdrawal_amount - actual_tx_fee` (the gas fee is permanently consumed on Ethereum).
6. User receives back their ckETH minus the gas fee.

---

### Impact Explanation

Any user who calls `withdraw_eth` targeting a smart contract address (e.g., a Gnosis Safe multisig, a DeFi vault, a smart contract wallet) will:
- Have their ckETH burned immediately and irreversibly at the time of the call.
- Receive a failed Ethereum transaction.
- Be reimbursed only `withdrawal_amount - actual_gas_fee`, permanently losing the Ethereum gas cost.

At current Ethereum gas prices, this loss can range from a few dollars to tens of dollars per failed withdrawal. There is no on-chain mechanism to warn or block the user before the burn occurs. The impact is a direct, quantifiable financial loss for any chain-fusion user withdrawing to a smart contract address.

---

### Likelihood Explanation

Smart contract wallets (Gnosis Safe, Argent, etc.) are extremely common among DeFi users — the exact demographic most likely to use ckETH. A user who holds ETH in a Gnosis Safe and wants to withdraw ckETH back to that same address will trigger this failure. The entry path requires no special privileges: any unprivileged caller can invoke `withdraw_eth` with a smart contract recipient address. The probability of real users hitting this is high.

---

### Recommendation

1. **Increase the default gas limit** for `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` to a value sufficient for smart contract `receive()` execution (e.g., 65,000 or a configurable value), similar to how `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is already set to 65,000.
2. **Alternatively**, expose a user-configurable `gas_limit` parameter in `WithdrawalArg` so callers can specify the gas needed for their recipient.
3. **At minimum**, reject `withdraw_eth` calls where the destination is a known smart contract (by querying the EVM RPC for the address's bytecode), preventing the burn before the transaction is doomed to fail.

---

### Proof of Concept

1. User holds 1 ckETH and wants to withdraw to their Gnosis Safe at `0xGnosisSafe...`.
2. User calls: `withdraw_eth({ amount: 1_000_000_000_000_000_000, recipient: "0xGnosisSafe..." })`.
3. Minter burns 1 ckETH from user's account (irreversible at this point).
4. Minter constructs EIP-1559 tx: `gas_limit = 21_000`, `value = 1 ETH - max_fee_estimate`.
5. Gnosis Safe's `receive()` function requires ~30,000+ gas for its internal logic → tx reverts out-of-gas on Ethereum.
6. Minter detects `TransactionStatus::Failure` in the receipt and reimburses `withdrawal_amount - actual_gas_fee`.
7. User receives back ~0.9998 ckETH, having lost ~0.0002 ETH worth of gas fees with no recourse.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1110-1145)
```rust
pub fn create_transaction(
    withdrawal_request: &WithdrawalRequest,
    nonce: TransactionNonce,
    gas_fee_estimate: GasFeeEstimate,
    gas_limit: GasAmount,
    ethereum_network: EthereumNetwork,
) -> Result<Eip1559TransactionRequest, CreateTransactionError> {
    assert!(
        gas_limit > GasAmount::ZERO,
        "BUG: gas limit should be non-zero"
    );
    match withdrawal_request {
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

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
