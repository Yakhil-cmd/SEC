### Title
Permanently Locked ETH Surplus from Unspent Transaction Fees with No Redistribution Mechanism - (File: `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter accumulates `total_unspent_tx_fees` — the difference between the maximum estimated gas fee charged to the user and the actual gas fee consumed on Ethereum — with every finalized withdrawal. This ETH surplus sits permanently in the minter's Ethereum address and is never redistributed, refunded, or used for any protocol purpose. The only operations on `total_unspent_tx_fees` are monotonic increments and reads for display/metrics. There is no mechanism to return this value to users or mint ckETH against it.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/state.rs`, the `EthBalance` struct tracks three fields:

```rust
pub struct EthBalance {
    eth_balance: Wei,
    total_effective_tx_fees: Wei,
    total_unspent_tx_fees: Wei,  // accumulates forever, never spent
}
``` [1](#0-0) 

Every time a withdrawal is finalized, `update_balance_upon_withdrawal()` computes:

```
unspent_tx_fee = charged_tx_fee - actual_tx_fee
```

and calls `total_unspent_tx_fees_add(unspent_tx_fee)`: [2](#0-1) 

The `total_unspent_tx_fees_add` function only ever increments the counter: [3](#0-2) 

Searching the entire minter codebase, `total_unspent_tx_fees` is used exclusively for dashboard display and Prometheus metrics: [4](#0-3) [5](#0-4) 

There is no `total_unspent_tx_fees_sub`, no governance endpoint to redistribute the surplus, and no mechanism to mint ckETH against it. The documentation explicitly acknowledges this surplus exists but provides no resolution path: [6](#0-5) 

---

### Impact Explanation

When a user calls `withdraw_eth`, the minter burns `withdrawal_amount` of ckETH from the user's ledger account and sends `withdrawal_amount - max_tx_fee_estimate` ETH to the Ethereum destination. The actual Ethereum gas cost is `actual_tx_fee < max_tx_fee_estimate`. The difference (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) remains in the minter's Ethereum address but is never accounted for as backing for any ckETH. Since the ckETH was already burned, this ETH is permanently orphaned:

- The ckETH supply is reduced by `withdrawal_amount`
- The ETH backing is only reduced by `withdrawal_amount - unspent_tx_fee`
- The surplus ETH accumulates in the minter's address with no claim path

Over time, the minter's Ethereum address holds more ETH than is needed to back the outstanding ckETH supply. This surplus is permanently inaccessible to users and has no defined protocol use. The impact is a **chain-fusion ledger conservation bug**: ETH value is extracted from users via ckETH burns but not fully delivered, with no refund or redistribution mechanism.

---

### Likelihood Explanation

**High.** The over-estimation of gas fees is intentional and occurs on every single finalized withdrawal (both ckETH and ckERC20). The minter documentation confirms this is by design to handle gas price volatility. Since every withdrawal generates unspent fees, the surplus grows continuously and deterministically with protocol usage. No special attacker action is required — normal user withdrawals via `withdraw_eth` or `withdraw_erc20` are sufficient to trigger accumulation. [7](#0-6) 

---

### Recommendation

Either:
1. **Implement a redistribution mechanism**: After each finalized withdrawal, mint ckETH equal to `unspent_tx_fee` back to the withdrawing user's account (analogous to how ckBTC reimburses failed withdrawals).
2. **Accumulate into a protocol treasury**: Route unspent fees to a designated fee-collector account on the ckETH ledger, governed by NNS proposals.
3. **Document and formalize the surplus as a protocol reserve**: If intentional, add a governance-controlled endpoint to redistribute or burn the surplus ETH on Ethereum, and track it separately from `eth_balance` to make the accounting invariant explicit.

The current state — where `total_unspent_tx_fees` is tracked but never acted upon — mirrors the `riskPoolBalance` pattern: funds are collected from users with no defined use or return path.

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount = 30_000_000_000_000_000` wei (minimum).
2. Minter estimates `max_tx_fee_estimate = 1_823_126_598_888_000` wei (example from docs).
3. Minter sends `28_176_873_401_112_000` wei to Ethereum destination.
4. Ethereum transaction mines with `actual_tx_fee = 899_399_014_248_000` wei.
5. `unspent_tx_fee = 1_823_126_598_888_000 - 899_399_014_248_000 = 923_727_584_640_000` wei (~0.00092 ETH) is added to `total_unspent_tx_fees`.
6. This ETH remains in the minter's Ethereum address. No ckETH is minted for it. No refund is issued. The counter only increments.
7. After N withdrawals, `total_unspent_tx_fees` = sum of all such surpluses, all permanently locked. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L355-375)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L704-727)
```rust
    fn total_unspent_tx_fees_add(&mut self, value: Wei) {
        self.total_unspent_tx_fees = self
            .total_unspent_tx_fees
            .checked_add(value)
            .unwrap_or_else(|| {
                panic!(
                    "BUG: overflow when adding {} to {}",
                    value, self.total_unspent_tx_fees
                )
            })
    }

    pub fn eth_balance(&self) -> Wei {
        self.eth_balance
    }

    pub fn total_effective_tx_fees(&self) -> Wei {
        self.total_effective_tx_fees
    }

    pub fn total_unspent_tx_fees(&self) -> Wei {
        self.total_unspent_tx_fees
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L991-995)
```rust
                w.encode_gauge(
                    "cketh_minter_total_unspent_tx_fees",
                    s.eth_balance.total_unspent_tx_fees().as_f64(),
                    "Total amount of unspent fees across all finalized transaction ckETH -> ETH",
                )?;
```

**File:** rs/ethereum/cketh/minter/templates/dashboard.html (L138-141)
```html
                    <tr id="total-unspent-tx-fees">
                        <th>Total unspent transaction fees (Wei)</th>
                        <td>{{ eth_balance.total_unspent_tx_fees() }}</td>
                    </tr>
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L216-223)
```text
[TIP]
.Effective transaction fees vs unspent transaction fees
====
The minter dashboard displays in the metadata table the following fees

. `Total effective transaction fees`: the sum of all `actual_tx_fee` for all withdrawals.
. `Total unspent transaction fees`: the sum of all `max_tx_fee_estimate - actual_tx_fee` for all withdrawals. This represents an overestimate of the actual transaction fees that were charged to the user but in retrospect not needed to mine the sent transaction.
====
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L229-237)
```text
. Initial withdrawal amount: `withdraw_amount:= 39_998_000_000_000_000` wei
. Gas limit: `21_000`
. Max fee per gas: `0x14369c3348 == 86_815_552_328` wei
. Maximum estimated transaction fees: `max_tx_fee_estimate:= 21_000 * 86_815_552_328 == 1_823_126_598_888_000` wei
. Amount received at destination: `39_998_000_000_000_000 - max_tx_fee_estimate == 38_174_873_401_112_000`
. Effective gas price: `0x9f8c76bc8 == 42_828_524_488` wei
. Actual transaction fee: `actual_tx_fee:= 21_000 * 42_828_524_488 == 899_399_014_248_000` wei
. Unspent transaction fee: `max_tx_fee_estimate - actual_tx_fee == 923_727_584_640_000` wei
. Amount charged at minter's address `withdrawal_amount - (max_tx_fee_estimate - actual_tx_fee) == 39_074_272_415_360_000` wei
```
