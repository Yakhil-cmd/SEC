### Title
Missing Minimum ETH Output (Slippage) Check in ckETH Minter `withdraw_eth` - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The `withdraw_eth` endpoint burns a user-specified amount of ckETH immediately and irreversibly, but computes the actual ETH delivered to the recipient later based on dynamic Ethereum gas prices. `WithdrawalArg` contains no `min_eth_out` field, so callers cannot enforce a minimum acceptable ETH output. By contrast, the `withdraw_erc20` path accepts a `max_transaction_fee` cap on the ckETH burned for gas, demonstrating that the developers recognise the need for slippage protection on the conversion path that uses a swap — but the direct ckETH withdrawal path has no equivalent guard.

### Finding Description
`withdraw_eth` in `rs/ethereum/cketh/minter/src/main.rs` immediately calls `client.burn_from(caller_account, amount, ...)` to destroy the caller's ckETH. [1](#0-0) 

The actual ETH delivered is `withdrawal_amount − max_tx_fee_estimate`, where `max_tx_fee_estimate` is computed asynchronously at transaction-creation time (typically ~6 minutes later) inside `create_transaction`. [2](#0-1) 

`create_transaction` only rejects the request when `max_tx_fee_estimate > withdrawal_amount` (i.e., the fee exceeds the entire amount). It does not enforce any user-supplied minimum output. [3](#0-2) 

`WithdrawalArg` carries only `amount`, `recipient`, and `from_subaccount` — no `min_eth_out` field. [4](#0-3) 

The asymmetry with `withdraw_erc20` is explicit: the ERC-20 path requires the caller to supply a `max_transaction_fee` that caps the ckETH burned for gas, giving the caller slippage protection. The ckETH path has no equivalent. [5](#0-4) 

### Impact Explanation
A caller who burns, say, 0.1 ckETH expecting to receive ~0.099 ETH (based on a pre-call fee estimate) may receive only 0.09 ETH if gas prices spike 10× between the `withdraw_eth` call and transaction creation. The ckETH is already destroyed and cannot be recovered. The caller has no mechanism to specify a minimum acceptable ETH output or to cancel the queued withdrawal. This is a direct, quantifiable financial loss for any unprivileged ingress caller.

### Likelihood Explanation
Ethereum gas prices routinely spike by an order of magnitude during network-congestion events (large NFT mints, DeFi liquidation cascades, etc.). The minter processes withdrawals asynchronously with a documented typical delay of ~6 minutes. Any caller who submits `withdraw_eth` immediately before or during such a spike is exposed. The scenario requires no privileged access and no attacker — it is triggered by ordinary user behaviour during normal market conditions.

### Recommendation
Add an optional `min_eth_out : opt nat` field to `WithdrawalArg`. At transaction-creation time in `create_transaction`, after computing `tx_amount = withdrawal_amount − max_tx_fee_estimate`, verify `tx_amount >= min_eth_out`. If the check fails, trigger the existing reimbursement mechanism (already used for `InsufficientTransactionFee`) to return the ckETH to the caller's account, minus a small processing fee.

### Proof of Concept
1. Caller queries `eip_1559_transaction_price` and observes `max_transaction_fee = 0.001 ETH`.
2. Caller submits `withdraw_eth(amount = 0.1 ETH, recipient = "0x…")`, expecting ~0.099 ETH.
3. The minter immediately burns 0.1 ckETH from the caller's ledger account.
4. Before the minter's next processing cycle, Ethereum base fee spikes 10×.
5. `create_transaction` computes `max_tx_fee_estimate = 0.01 ETH`; since `0.1 − 0.01 = 0.09 > 0`, no error is raised.
6. The recipient receives 0.09 ETH — 9% less than the caller anticipated — with no recourse.
7. Had a `min_eth_out = 0.098 ETH` guard existed, the transaction would have been rejected and the ckETH reimbursed.

### Citations

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1123-1134)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1168)
```rust
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
            }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L298-307)
```text
type WithdrawalArg = record {
    // The address to which the minter should deposit ETH.
    recipient : text;

    // The amount of ckETH in Wei that the client wants to withdraw.
    amount : nat;

    // The subaccount to burn ckETH from.
    from_subaccount : opt Subaccount;
};
```
