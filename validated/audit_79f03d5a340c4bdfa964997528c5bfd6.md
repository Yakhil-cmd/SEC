### Title
Fixed Gas Limit of 21,000 Causes ckETH Withdrawals to Smart Contract Addresses to Fail with Permanent Loss of Transaction Fees - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary
The ckETH minter hardcodes a gas limit of `21,000` for all `withdraw_eth` transactions. This is sufficient only for simple ETH transfers to externally-owned accounts (EOAs). Any user who calls `withdraw_eth` with a smart contract address as the recipient (e.g., a multisig wallet, a DeFi protocol, or a proxy contract) will have their Ethereum transaction fail on-chain. Because the ckETH is burned before the transaction is sent, and the reimbursement only returns `withdrawal_amount - effective_transaction_fee`, the user permanently loses the gas fees paid for the failed transaction.

---

### Finding Description

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is hardcoded to `21_000` in `rs/ethereum/cketh/minter/src/withdraw.rs`:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns this constant for all CkEth withdrawal requests, regardless of whether the destination is an EOA or a smart contract:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This fixed limit is then passed directly into `create_transaction`, which builds the EIP-1559 transaction submitted to Ethereum: [3](#0-2) 

The `withdraw_eth` endpoint in `rs/ethereum/cketh/minter/src/main.rs` accepts any non-blocked Ethereum address as a recipient. The `validate_address_as_destination` call only checks the blocklist — it does not distinguish between EOAs and smart contracts: [4](#0-3) 

The DID interface itself acknowledges this limitation:

```
// IMPORTANT: The current gas limit is set to 21,000 for a transaction
// so withdrawals to smart contract addresses will likely fail.
withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
``` [5](#0-4) 

Despite this documented warning, the code does not enforce any restriction preventing users from submitting withdrawals to smart contract addresses. The withdrawal is accepted, the ckETH is burned from the ledger, the transaction is sent with 21,000 gas, and it fails on-chain.

---

### Impact Explanation

The withdrawal flow is:
1. User calls `withdraw_eth` with a smart contract address as recipient.
2. The minter burns `withdrawal_amount` of ckETH from the user's ledger account.
3. The minter constructs an EIP-1559 transaction with `gas_limit = 21_000` and `value = withdrawal_amount - max_tx_fee_estimate`.
4. The transaction is submitted to Ethereum. Because the recipient is a smart contract requiring more than 21,000 gas to receive ETH, the transaction fails (status = `Failure`).
5. The minter reimburses the user `withdrawal_amount - effective_transaction_fee` in ckETH.

The user permanently loses `effective_transaction_fee` worth of ckETH — the actual gas cost of the failed transaction on Ethereum. For a typical Ethereum transaction at moderate gas prices, this can be on the order of tens of thousands of wei to several million wei, representing real monetary loss. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged user can call `withdraw_eth` with a smart contract address as the recipient. Common real-world scenarios include:
- Withdrawing to a Gnosis Safe or other multisig wallet (requires >21,000 gas to receive ETH).
- Withdrawing to a DeFi protocol's deposit contract.
- Withdrawing to a proxy contract.

The entry path is fully permissionless and requires no special role. The only prerequisite is holding ckETH and an ICRC-2 approval on the ledger. The likelihood is **medium-high** for users who are not aware of the limitation or who misread the warning in the DID.

---

### Recommendation

1. **Reject withdrawals to smart contract addresses at the minter level**: Before accepting a `withdraw_eth` request, the minter could query the Ethereum JSON-RPC `eth_getCode` endpoint for the recipient address. If the address has non-empty bytecode, the request should be rejected with a clear error (e.g., `RecipientIsSmartContract`), preventing the user from burning ckETH for a transaction that will inevitably fail.

2. **Alternatively, allow a user-specified gas limit**: Expose an optional `gas_limit` parameter in `WithdrawalArg` so users who know their recipient is a smart contract can supply a sufficient gas limit. Apply a reasonable maximum cap to prevent abuse.

3. **At minimum, enforce the documented restriction in code**: If the intent is to only support EOA recipients, add an explicit validation step that rejects smart contract addresses before burning ckETH, rather than relying solely on documentation.

---

### Proof of Concept

1. User holds ckETH and calls `icrc2_approve` on the ckETH ledger to allow the minter to burn their tokens.
2. User calls `withdraw_eth` on the minter with `recipient = "0x<gnosis_safe_address>"` (a multisig smart contract).
3. The minter accepts the request, burns the user's ckETH, and emits `AcceptedEthWithdrawalRequest`.
4. The minter's timer fires `process_retrieve_eth_requests`, which calls `estimate_gas_limit` → returns `21_000`, then `create_transaction` → builds an EIP-1559 tx with `gas_limit = 21_000`.
5. The transaction is signed via threshold ECDSA and submitted to Ethereum via `send_raw_transaction`.
6. The Gnosis Safe's `receive` or `fallback` function consumes more than 21,000 gas; the transaction reverts with `status = Failure`.
7. The minter detects the failure in `finalize_transactions_batch`, records `FinalizedTransaction` with `TransactionStatus::Failure`, and schedules a reimbursement.
8. `process_reimbursement` mints back `withdrawal_amount - effective_transaction_fee` to the user.
9. The user has lost `effective_transaction_fee` worth of ckETH with no ETH received at the destination. [1](#0-0) [2](#0-1) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-264)
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

**File:** rs/ethereum/cketh/minter/src/state/tests.rs (L1439-1445)
```rust
        assert_eq!(
            eth_balance_after_failed_withdrawal.eth_balance,
            eth_balance_before_withdrawal
                .eth_balance
                .checked_sub(receipt_failed.effective_transaction_fee())
                .unwrap()
        );
```
