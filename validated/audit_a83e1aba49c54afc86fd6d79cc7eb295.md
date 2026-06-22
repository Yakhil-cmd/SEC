### Title
Hardcoded 21,000 Gas Limit in ckETH Minter Causes ETH Withdrawals to Smart Contract Addresses to Fail with Guaranteed Gas Fee Loss - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary

The ckETH minter hardcodes `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 21_000` for all ETH withdrawal transactions. This is the direct IC analog to M-7's use of Solidity `transfer` (2,300 gas stipend): a fixed gas ceiling that is sufficient only for EOA recipients but always causes out-of-gas failures when the recipient is a smart contract. Because ckETH is burned before the Ethereum transaction is submitted, and the reimbursement on failure deducts the `effective_transaction_fee` paid on Ethereum, users who withdraw to a smart contract address suffer a guaranteed, irreversible loss of the gas fee.

### Finding Description

`CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is defined as a compile-time constant of `21_000` gas units: [1](#0-0) 

This constant is selected unconditionally for every ckETH withdrawal via `estimate_gas_limit`: [2](#0-1) 

The `create_transaction` function embeds this limit directly into the EIP-1559 transaction that is signed via threshold ECDSA and broadcast to Ethereum: [3](#0-2) 

The `withdraw_eth` endpoint burns ckETH from the caller **before** the Ethereum transaction is created or sent: [4](#0-3) 

The only address validation performed is a blocklist check; there is no guard against smart contract addresses: [5](#0-4) 

When the Ethereum transaction fails (out of gas against a smart contract recipient), the reimbursement amount is `withdrawal_amount - effective_fee_paid`, confirmed by the transaction-state machine test: [6](#0-5) 

The integration test quantifies the concrete loss: `693_077_873_418_000 wei` (~0.00069 ETH) is permanently lost per failed withdrawal at the gas prices used in the test: [7](#0-6) 

The DID interface acknowledges the limitation but does not prevent the call: [8](#0-7) 

### Impact Explanation

Any user who calls `withdraw_eth` with a smart contract address as the recipient (e.g., a Gnosis Safe multisig, a DeFi vault, an exchange hot wallet) will:

1. Have their ckETH burned immediately and irreversibly on the IC ledger.
2. Receive an Ethereum transaction that reverts out-of-gas because 21,000 gas is insufficient for any smart contract `receive`/`fallback`.
3. Be reimbursed only `withdrawal_amount − effective_gas_fee`, permanently losing the gas cost of the failed transaction.

The loss per event is `21_000 × effective_gas_price`. At 20 gwei this is ~0.00042 ETH; at 100 gwei it exceeds 0.002 ETH. The loss is deterministic and unavoidable once the call is accepted.

### Likelihood Explanation

Smart contract addresses are common withdrawal destinations: multisig wallets (Gnosis Safe), exchange deposit contracts, and DeFi protocols all use smart contracts. A user who holds ckETH and wants to withdraw to any such address will trigger this path. The `withdraw_eth` endpoint is publicly callable by any non-anonymous principal with no additional preconditions beyond holding sufficient ckETH and an approval.

### Recommendation

Replace the hardcoded constant with a configurable or dynamically estimated gas limit, or add a pre-flight check that rejects withdrawals to known smart contract addresses (by querying the Ethereum node for `eth_getCode` via HTTPS outcalls before accepting the burn). At minimum, the `withdraw_eth` endpoint should return a structured error (rather than silently accepting the burn) when the destination is detected to be a contract.

### Proof of Concept

1. User holds ckETH and calls `icrc2_approve(minter, amount)` on the ckETH ledger.
2. User calls `withdraw_eth({ amount: X, recipient: "0x<smart_contract_address>" })`.
3. Minter burns `X` ckETH from the user's account.
4. Minter constructs an EIP-1559 transaction with `gas_limit = 21_000` and broadcasts it.
5. Ethereum executes the transaction; the smart contract's `receive` function consumes more than 21,000 gas → transaction reverts with out-of-gas.
6. Minter detects `TransactionStatus::Failure` in the receipt, computes `reimbursed_amount = X − (21_000 × effective_gas_price)`, and mints that amount back.
7. User has permanently lost `21_000 × effective_gas_price` wei of ckETH with no ETH received.

The existing integration test `should_reimburse` in `rs/ethereum/cketh/minter/tests/cketh.rs` already demonstrates this exact flow with a failed transaction, confirming the gas fee loss of `693_077_873_418_000 wei`. [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-313)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1731-1736)
```rust
                    reimbursed_amount: withdrawal_request
                        .withdrawal_amount
                        .checked_sub(effective_fee_paid)
                        .unwrap()
                        .change_units()
                }
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L438-516)
```rust
#[test]
fn should_reimburse() {
    let cketh = CkEthSetup::default();
    let minter: Principal = cketh.minter_id.into();
    let caller: Principal = cketh.caller.into();
    let withdrawal_amount = Nat::from(CKETH_WITHDRAWAL_AMOUNT);
    let destination = "0x221E931fbFcb9bd54DdD26cE6f5e29E98AdD01C0".to_string();

    let cketh = cketh
        .deposit(DepositParams::default())
        .expect_mint()
        .call_ledger_get_transaction(0_u8)
        .expect_mint(Mint {
            amount: EXPECTED_BALANCE.into(),
            to: Account {
                owner: PrincipalId::new_user_test_id(DEFAULT_PRINCIPAL_ID).into(),
                subaccount: None,
            },
            memo: Some(Memo::from(MintMemo::Convert {
                from_address: DEFAULT_DEPOSIT_FROM_ADDRESS.parse().unwrap(),
                tx_hash: DEFAULT_DEPOSIT_TRANSACTION_HASH.parse().unwrap(),
                log_index: DEFAULT_DEPOSIT_LOG_INDEX.into(),
            })),
            created_at_time: None,
            fee: None,
        })
        .call_ledger_approve_minter(caller, EXPECTED_BALANCE, None)
        .expect_ok(1);

    let balance_before_withdrawal = cketh.balance_of(caller);
    assert_eq!(balance_before_withdrawal, withdrawal_amount);

    // advance time so that time does not grow implicitly when executing a round
    cketh.env.advance_time(Duration::from_secs(1));
    let time_at_withdrawal = cketh.env.get_time().as_nanos_since_unix_epoch();

    let cketh = cketh
        .call_minter_withdraw_eth(caller, withdrawal_amount.clone(), destination.clone())
        .expect_withdrawal_request_accepted();

    let withdrawal_id = cketh.withdrawal_id().clone();
    let (tx, _sig) = default_signed_eip_1559_transaction();
    let cketh = cketh
        .wait_and_validate_withdrawal(
            ProcessWithdrawalParams::default().with_failed_transaction_receipt(),
        )
        .expect_finalized_status(TxFinalizedStatus::PendingReimbursement(EthTransaction {
            transaction_hash: DEFAULT_WITHDRAWAL_TRANSACTION_HASH.to_string(),
        }))
        .call_ledger_get_transaction(withdrawal_id.clone())
        .expect_burn(Burn {
            amount: withdrawal_amount.clone(),
            from: Account {
                owner: PrincipalId::new_user_test_id(DEFAULT_PRINCIPAL_ID).into(),
                subaccount: None,
            },
            spender: Some(Account {
                owner: minter,
                subaccount: None,
            }),
            memo: Some(Memo::from(BurnMemo::Convert {
                to_address: destination.parse().unwrap(),
            })),
            created_at_time: None,
            fee: None,
        });

    assert_eq!(cketh.balance_of(caller), Nat::from(0_u8));

    cketh.env.advance_time(PROCESS_REIMBURSEMENT);
    cketh.env.tick();

    let cost_of_failed_transaction = withdrawal_amount
        .0
        .to_u128()
        .unwrap()
        .checked_sub(tx.value.unwrap().as_u128())
        .unwrap();
    assert_eq!(cost_of_failed_transaction, 693_077_873_418_000);
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-720)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });
```
