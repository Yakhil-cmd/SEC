### Title
ckETH `withdraw_eth` Hardcoded 21,000 Gas Limit Causes Guaranteed ETH Transaction Failure and Irrecoverable Gas Fee Loss When Withdrawing to Smart Contract Addresses - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `withdraw_eth` endpoint uses a hardcoded gas limit of `21,000` for every ETH withdrawal transaction. This is the exact minimum required for a plain ETH transfer to an EOA (externally owned account) and is insufficient for any Ethereum smart contract recipient that executes logic in its `receive` or `fallback` function. Any user who calls `withdraw_eth` targeting a smart contract address (e.g., a multisig, a DeFi vault, a DAO treasury) will have their ckETH burned, the resulting Ethereum transaction will fail on-chain due to out-of-gas, and the user will permanently lose the gas fee (`21,000 × effective_gas_price` wei) with no recourse beyond the partial reimbursement.

---

### Finding Description

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is a compile-time constant set to `21_000`:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
```

The `withdraw_eth` handler in `rs/ethereum/cketh/minter/src/main.rs` accepts any syntactically valid Ethereum address as `recipient`. It performs only a blocklist check via `validate_address_as_destination`; it does **not** distinguish between EOA and contract addresses. It then immediately and irreversibly burns the caller's ckETH on the ledger:

```rust
match client
    .burn_from(Account { owner: caller, subaccount: from_subaccount }, amount, BurnMemo::Convert { to_address: destination })
    .await
{
    Ok(ledger_burn_index) => { /* queue withdrawal */ }
    Err(e) => Err(WithdrawalError::from(e)),
}
```

The minter subsequently constructs an EIP-1559 transaction with `gas_limit = CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` (21,000). When this transaction is submitted to Ethereum and the recipient is a smart contract whose `receive`/`fallback` function consumes more than 21,000 gas (which is virtually every non-trivial contract), the transaction reverts on-chain with an out-of-gas error.

The minter detects the `TransactionStatus::Failure` receipt and schedules a reimbursement of `withdrawal_amount − effective_tx_fee`. The user recovers most of their ckETH but permanently loses `effective_tx_fee = 21_000 × effective_gas_price`. At 50 gwei this is ~0.001 ETH; at 200 gwei it exceeds 0.004 ETH per failed withdrawal.

The DID interface itself acknowledges the problem in a comment but does not enforce it:

```
// IMPORTANT: The current gas limit is set to 21,000 for a transaction
// so withdrawals to smart contract addresses will likely fail.
withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```

No on-chain or off-chain validation prevents the minter from accepting and processing such a request.

---

### Impact Explanation

Any unprivileged IC principal can call `withdraw_eth` with a smart contract address as the recipient. The ckETH burn is atomic and irreversible. The Ethereum transaction will deterministically fail. The user loses `21,000 × effective_gas_price` wei of ETH value (deducted from the reimbursed ckETH amount). For users withdrawing to multisig wallets, DAO treasuries, or DeFi contracts — all common patterns — this is a guaranteed, repeatable financial loss per withdrawal attempt. The minter's own documentation acknowledges the failure mode but provides no enforcement.

---

### Likelihood Explanation

Smart contract addresses are the dominant recipients for institutional and DeFi users of ckETH. Gnosis Safe multisigs, Compound/Aave treasury addresses, and DAO governance contracts all require more than 21,000 gas to receive ETH. Any such user who calls `withdraw_eth` without carefully reading the DID comment will trigger the failure. The entry path requires no special privilege: a standard `icrc2_approve` followed by `withdraw_eth` is sufficient.

---

### Recommendation

1. **Enforce at acceptance time**: Before burning ckETH, query the Ethereum node (via the existing HTTPS outcall infrastructure) to determine whether the recipient address has deployed bytecode (`eth_getCode`). Reject the withdrawal with a descriptive error if the address is a contract and the gas limit would be insufficient.
2. **Alternatively, increase the gas limit**: Use a higher default gas limit (e.g., 65,000, already used for `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT`) for ETH withdrawals, or make it user-configurable with a cap.
3. **At minimum, surface the warning as a hard error**: Return `WithdrawalError::TemporarilyUnavailable` or a new `RecipientIsContract` variant rather than silently accepting the request and burning funds.

---

### Proof of Concept

1. User holds ckETH and calls `icrc2_approve(minter, amount)` on the ckETH ledger.
2. User calls `withdraw_eth({ amount: X, recipient: "0x<gnosis_safe_address>" })` on the ckETH minter.
3. Minter burns `X` ckETH from the user's account (irreversible).
4. Minter constructs an EIP-1559 transaction with `gas_limit = 21_000` to the Gnosis Safe address.
5. Ethereum executes the transaction; the Safe's `receive` function requires ~6,900 gas for its proxy dispatch, exceeding the 21,000 limit when combined with base EVM overhead for a contract call — the transaction reverts with `out of gas`.
6. Minter observes `TransactionStatus::Failure` in the receipt and schedules reimbursement of `X − (21_000 × effective_gas_price)`.
7. User receives back `X − effective_tx_fee` ckETH. The gas fee is permanently lost.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-117)
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
