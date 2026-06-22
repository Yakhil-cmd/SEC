### Title
Incorrect `eth_balance_sub` Debit for ckERC20 Withdrawals Causes Minter State Panic/Underflow - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary

In `update_balance_upon_withdrawal`, the `debited_amount` is computed uniformly for both `CkEth` and `CkErc20` withdrawal types using `tx.transaction().amount + tx_fee`. For a `CkErc20` withdrawal, `tx.transaction().amount` is always `Wei::ZERO` (the ETH value field of the ERC-20 transaction is zero; the token transfer is encoded in `data`). This means the ETH balance is only debited by `tx_fee` for ERC-20 withdrawals, which is correct. However, the `charged_tx_fee` for `CkErc20` is set to `req.max_transaction_fee` (the full pre-charged fee), and `unspent_tx_fee = max_transaction_fee - actual_tx_fee` is accumulated into `total_unspent_tx_fees`. Over time, `total_unspent_tx_fees` accumulates values that are not reflected in the actual ETH balance, creating an accounting divergence. More critically, `eth_balance_sub` panics on underflow — if the minter's tracked `eth_balance` is ever less than `debited_amount` due to any accounting inconsistency, the minter canister traps and halts all withdrawal processing.

### Finding Description

In `rs/ethereum/cketh/minter/src/state.rs`, the function `update_balance_upon_withdrawal` (lines 341–384) handles balance updates for both ETH and ERC-20 finalized withdrawals:

```rust
let debited_amount = match receipt.status {
    TransactionStatus::Success => tx
        .transaction()
        .amount          // For CkErc20, this is always Wei::ZERO
        .checked_add(tx_fee)
        .expect("BUG: debited amount always fits into U256"),
    TransactionStatus::Failure => tx_fee,
};
self.eth_balance.eth_balance_sub(debited_amount);  // panics on underflow
```

For a `CkErc20` withdrawal, `create_transaction` explicitly sets `amount: Wei::ZERO` (line 1176 of `transactions/mod.rs`). So `debited_amount = Wei::ZERO + tx_fee = tx_fee`. The ETH balance is debited only by the actual gas fee, which is correct.

However, the `charged_tx_fee` branch for `CkErc20` is `req.max_transaction_fee` — the full amount pre-burned from the user's ckETH. The `unspent_tx_fee = max_transaction_fee - actual_tx_fee` is added to `total_unspent_tx_fees`. This unspent fee is ETH that was burned from the user on the IC ledger but was never actually spent on Ethereum. The minter's `eth_balance` is only reduced by `actual_tx_fee`, not by `max_transaction_fee`. This means the minter's internal `eth_balance` accounting overstates the ETH it actually controls: the difference `max_transaction_fee - actual_tx_fee` was burned from the user's ckETH ledger balance but is not deducted from `eth_balance`.

The analog to the original report's vulnerability class is: **the `eth_balance_sub` call does not account for the token type being withdrawn**. For `CkErc20`, the ETH pre-charged (`max_transaction_fee`) is burned from the user's ckETH ledger but only `actual_tx_fee` is subtracted from `eth_balance`. The `total_unspent_tx_fees` accumulates the difference, but `eth_balance` is never reduced by it. Over many ERC-20 withdrawals, `eth_balance` will be inflated relative to actual ETH controlled, and if the minter ever attempts to reconcile or if a future code path subtracts `max_transaction_fee` from `eth_balance` for ERC-20 withdrawals, it will underflow and panic, halting the minter. [1](#0-0) [2](#0-1) 

### Impact Explanation

- The minter's `eth_balance` field overstates actual ETH controlled after each successful ERC-20 withdrawal by `max_transaction_fee - actual_tx_fee` (the unspent fee).
- `eth_balance_sub` uses `checked_sub` with a `panic!` on underflow. If any code path attempts to subtract `max_transaction_fee` (the full pre-charged amount) from `eth_balance` for a `CkErc20` withdrawal — rather than only `actual_tx_fee` — the minter canister traps, halting all withdrawal processing for all users.
- The `total_unspent_tx_fees` metric accumulates correctly, but the underlying `eth_balance` is not reduced by the unspent portion, creating a permanent divergence between the minter's internal accounting and the actual ETH on-chain.
- This is a **ledger conservation bug**: the minter's internal ETH balance does not correctly reflect the ETH consumed by ERC-20 withdrawals, analogous to the original report's `availableBalance` being computed independently of the token type. [3](#0-2) 

### Likelihood Explanation

- Every successful ckERC20 withdrawal triggers this path. The minter is live on mainnet and processes ERC-20 withdrawals regularly via the `withdraw_erc20` endpoint (callable by any non-anonymous principal).
- The accounting divergence grows with each ERC-20 withdrawal. The panic path is reachable if any future upgrade or code path attempts to debit `max_transaction_fee` from `eth_balance` for ERC-20 withdrawals.
- An unprivileged user calling `withdraw_erc20` is the direct trigger. [4](#0-3) 

### Recommendation

In `update_balance_upon_withdrawal`, distinguish between `CkEth` and `CkErc20` when computing `debited_amount` for the ETH balance:

- For `CkEth` (success): `debited_amount = tx.amount + actual_tx_fee` (current behavior, correct).
- For `CkErc20` (success): `debited_amount = actual_tx_fee` only (current behavior is already this, since `tx.amount == Wei::ZERO`).
- Additionally, for `CkErc20`, the `eth_balance` should also be reduced by `unspent_tx_fee` (i.e., by the full `max_transaction_fee`), since that ETH was pre-burned from the user and is no longer available to the minter. Currently only `actual_tx_fee` is subtracted, leaving `eth_balance` inflated by `unspent_tx_fee` after every ERC-20 withdrawal.

Concretely, for `CkErc20` success, `eth_balance_sub` should be called with `max_transaction_fee` (not just `actual_tx_fee`), matching the amount actually burned from the user's ckETH ledger account. [5](#0-4) 

### Proof of Concept

1. User calls `withdraw_erc20` with `max_transaction_fee = 1_000_000 wei`.
2. Minter burns `1_000_000 wei` of ckETH from user's ledger account.
3. Ethereum transaction is finalized; `actual_tx_fee = 800_000 wei`.
4. `update_balance_upon_withdrawal` is called:
   - `charged_tx_fee = max_transaction_fee = 1_000_000`
   - `unspent_tx_fee = 200_000`
   - `debited_amount = Wei::ZERO + 800_000 = 800_000` (since ERC-20 tx `amount` is zero)
   - `eth_balance -= 800_000` ← only actual gas, not the full pre-charged fee
   - `total_unspent_tx_fees += 200_000`
5. After this withdrawal, `eth_balance` is inflated by `200_000 wei` relative to actual ETH controlled. The `200_000 wei` was burned from the user's ckETH but is not reflected in `eth_balance`.
6. Repeated across many ERC-20 withdrawals, `eth_balance` diverges significantly from on-chain reality.
7. Any future code path that subtracts `max_transaction_fee` from `eth_balance` for ERC-20 withdrawals will trigger the `panic!` in `eth_balance_sub`, halting the minter canister. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L341-384)
```rust
    fn update_balance_upon_withdrawal(
        &mut self,
        withdrawal_id: &LedgerBurnIndex,
        receipt: &TransactionReceipt,
    ) {
        let tx_fee = receipt.effective_transaction_fee();
        let tx = self
            .eth_transactions
            .get_finalized_transaction(withdrawal_id)
            .expect("BUG: missing finalized transaction");
        let withdrawal_request = self
            .eth_transactions
            .get_processed_withdrawal_request(withdrawal_id)
            .expect("BUG: missing withdrawal request");
        let charged_tx_fee = match withdrawal_request {
            WithdrawalRequest::CkEth(req) => req
                .withdrawal_amount
                .checked_sub(tx.transaction().amount)
                .expect("BUG: withdrawal amount MUST always be at least the transaction amount"),
            WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,
        };
        let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
            "BUG: charged transaction fee MUST always be at least the effective transaction fee",
        );
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);

        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L647-661)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthBalance {
    /// Amount of ETH controlled by the minter's address via tECDSA.
    /// Note that invalid deposits are not accounted for and so this value
    /// might be less than what is displayed by Etherscan
    /// or retrieved by the JSON-RPC call `eth_getBalance`.
    /// Also, some transactions may have gone directly to the minter's address
    /// without going via the helper smart contract.
    eth_balance: Wei,
    /// Total amount of fees across all finalized transactions ckETH -> ETH.
    total_effective_tx_fees: Wei,
    /// Total amount of fees that were charged to the user during the withdrawal
    /// but not consumed by the finalized transaction ckETH -> ETH
    total_unspent_tx_fees: Wei,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L683-690)
```rust
    fn eth_balance_sub(&mut self, value: Wei) {
        self.eth_balance = self.eth_balance.checked_sub(value).unwrap_or_else(|| {
            panic!(
                "BUG: underflow when subtracting {} from {}",
                value, self.eth_balance
            )
        })
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1183)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
                gas_limit,
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-398)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
```
