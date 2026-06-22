### Title
Fixed 21,000 Gas Limit in `withdraw_eth` Causes Guaranteed Fund Loss When Withdrawing ckETH to Smart Contract Addresses - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `withdraw_eth` endpoint uses a hardcoded gas limit of `21,000` for all ETH withdrawal Ethereum transactions. This is the minimum gas for a simple EOA-to-EOA transfer. When a user withdraws ckETH to a smart contract address (multisig wallet, DeFi protocol, any contract with a non-trivial `receive`/`fallback`), the Ethereum transaction will fail out-of-gas. The user's ckETH is already burned at that point, and the reimbursement is `withdrawal_amount − effective_gas_fee`, meaning the user permanently loses the gas fee paid for the failed transaction.

---

### Finding Description

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is hardcoded to `21_000` in `rs/ethereum/cketh/minter/src/withdraw.rs`: [1](#0-0) 

This constant is used when constructing every ckETH withdrawal Ethereum transaction via `create_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`: [2](#0-1) 

The `withdraw_eth` update method in `rs/ethereum/cketh/minter/src/main.rs` burns the user's ckETH **before** the Ethereum transaction is even created, then queues the withdrawal: [3](#0-2) 

The `validate_address_as_destination` call only checks the blocklist and address validity — it does **not** detect whether the recipient is a smart contract: [4](#0-3) 

The DID interface acknowledges the limitation in a comment but does not enforce any restriction: [5](#0-4) 

When the Ethereum transaction fails (status `Failure`), the minter creates a reimbursement for `withdrawal_amount − effective_transaction_fee`, confirmed by the test: [6](#0-5) 

The user permanently loses the gas fee paid for the failed transaction.

---

### Impact Explanation

Any user who calls `withdraw_eth` specifying a smart contract address as `recipient` will:

1. Have their ckETH burned (irreversible once the ledger burn succeeds).
2. Have the Ethereum transaction fail out-of-gas (21,000 gas is insufficient for any contract with a non-trivial `receive`/`fallback`).
3. Receive a reimbursement of `withdrawal_amount − effective_gas_fee`, losing the gas fee permanently.

At typical Ethereum gas prices this loss is on the order of 0.0007–0.002 ETH per failed withdrawal. For users withdrawing to Gnosis Safe multisigs, DeFi vaults, or any smart-contract wallet, this is a guaranteed, repeatable financial loss. The ckETH ledger conservation invariant is violated: more ckETH is burned than ETH is delivered.

---

### Likelihood Explanation

Smart contract addresses are extremely common withdrawal destinations: Gnosis Safe multisigs, Uniswap LP positions, DeFi yield vaults, and smart-contract wallets are all standard recipients. Any user who withdraws ckETH to such an address will trigger this loss. The warning exists only in the `.did` file comment; third-party frontends and integrations are unlikely to surface it. The entry path requires no privilege — any non-anonymous principal can call `withdraw_eth`.

---

### Recommendation

1. **Increase the gas limit** for ckETH withdrawals to at least 65,000 (already used for ckERC20 withdrawals per `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT`) to accommodate standard smart contract `receive` functions.
2. **Or** add an on-chain check (via `eth_getCode`) before accepting the withdrawal request to detect contract addresses and either reject them or apply a higher gas limit.
3. At minimum, surface the warning prominently in the `withdraw_eth` response payload rather than only in the DID comment, so all callers — not just those reading the interface file — are informed before funds are burned.

---

### Proof of Concept

1. User holds 0.1 ckETH and calls `withdraw_eth` with `recipient = "0x<GnosisSafe address>"`.
2. The minter calls `burn_from` on the ckETH ledger — 0.1 ckETH is burned.
3. The minter constructs an EIP-1559 transaction with `gas_limit = 21_000` to the Gnosis Safe address.
4. Gnosis Safe's `receive` function requires ~6,000+ additional gas beyond the base 21,000 intrinsic cost; the transaction reverts with out-of-gas.
5. The minter observes `TransactionStatus::Failure` and creates a `ReimbursementRequest` for `0.1 ETH − effective_gas_fee`.
6. The user receives back `≈ 0.0993 ckETH`; the gas fee (~0.0007 ETH equivalent in ckETH) is permanently lost.

The hardcoded constant and the burn-before-send ordering are the necessary vulnerable steps in the IC production code path.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-336)
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
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L510-522)
```rust
    let cost_of_failed_transaction = withdrawal_amount
        .0
        .to_u128()
        .unwrap()
        .checked_sub(tx.value.unwrap().as_u128())
        .unwrap();
    assert_eq!(cost_of_failed_transaction, 693_077_873_418_000);

    let balance_after_withdrawal = cketh.balance_of(caller);
    assert_eq!(
        balance_after_withdrawal,
        balance_before_withdrawal.clone() - cost_of_failed_transaction
    );
```
