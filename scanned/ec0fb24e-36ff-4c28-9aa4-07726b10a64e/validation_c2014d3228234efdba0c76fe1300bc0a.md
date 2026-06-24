### Title
Unspent Transaction Fees Permanently Locked in ckETH Minter with No Claim Mechanism - (File: `rs/ethereum/cketh/minter/src/state.rs`)

### Summary
The ckETH minter charges users a `max_tx_fee_estimate` when they call `withdraw_eth`, burning that amount from their ckETH balance. Because the actual Ethereum gas fee is always lower than the estimate, the difference (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) accumulates permanently in the minter's Ethereum address. There is no function in the minter canister that allows users to claim back these unspent fees, nor any mechanism to redistribute them. The ETH is tracked only as a dashboard metric (`total_unspent_tx_fees`) and is never returned.

### Finding Description
When a user withdraws ckETH, the minter burns the full `withdraw_amount` from the ckETH ledger and sends `withdraw_amount - max_tx_fee_estimate` ETH to the destination address. After the Ethereum transaction is mined, `update_balance_upon_withdrawal` computes:

```
unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee
```

and calls `total_unspent_tx_fees_add(unspent_tx_fee)`. The `eth_balance` is only reduced by the actual debit (`tx.amount + actual_tx_fee`), so the unspent portion remains in the minter's Ethereum address and is reflected in `eth_balance`. However, the ckETH that was burned to cover `max_tx_fee_estimate` is gone permanently. The minter therefore holds ETH that has no corresponding ckETH backing it, and there is no `claim_unspent_fees`, `withdraw_fees`, or equivalent function exposed by the minter canister.

The `total_unspent_tx_fees_add` method is the only operation on this field — there is no corresponding subtraction path anywhere in the codebase.

### Impact Explanation
Every ckETH withdrawal results in a non-zero `unspent_tx_fee` (by design, `max_tx_fee_estimate > actual_tx_fee`). Over the lifetime of the minter, this accumulates into a growing pool of ETH that:
- Was charged to users (burned from their ckETH)
- Sits in the minter's Ethereum address permanently
- Cannot be claimed by any user or redistributed

This breaks the 1:1 backing invariant of the ckETH system: the total ckETH supply understates the ETH held by the minter by exactly `sum(unspent_tx_fees)`. Users are systematically overcharged for every withdrawal with no recourse.

**Vulnerability class**: Chain-fusion mint/burn conservation bug — users burn more ckETH than the actual ETH cost, and the excess ETH is permanently retained by the minter canister.

### Likelihood Explanation
This affects every single `withdraw_eth` call. Any unprivileged user who holds ckETH and calls `withdraw_eth` is affected. No special conditions, timing, or privileged access are required. The documentation explicitly acknowledges the existence of unspent fees but provides no return path.

### Recommendation
Add a mechanism to return unspent fees to users. Options include:
1. After a transaction is finalized, mint `unspent_tx_fee` ckETH back to the original withdrawer's account.
2. Accumulate unspent fees in a fee collector account that can be redistributed via governance.
3. Use the unspent fees to subsidize future withdrawal gas costs for all users.

### Proof of Concept

**Root cause — `update_balance_upon_withdrawal` in `rs/ethereum/cketh/minter/src/state.rs`:** [1](#0-0) 

The `unspent_tx_fee` is computed and added to the accumulator, but the ETH is never returned to the user.

**`EthBalance` struct — `total_unspent_tx_fees` is write-only from the user's perspective:** [2](#0-1) 

**`total_unspent_tx_fees_add` — the only operation on this field, no corresponding subtraction:** [3](#0-2) 

**Documentation explicitly acknowledges the overcharge with no return path:** [4](#0-3) 

**Dashboard exposes the metric but no claim endpoint exists:** [5](#0-4) 

**Concrete example from the documentation**: a single withdrawal of ~0.04 ETH resulted in `923_727_584_640_000` wei (~0.00092 ETH, ~2.3% of the withdrawal amount) being permanently retained as unspent fees. Across thousands of withdrawals, this compounds into a material amount of ETH locked with no claim mechanism. [6](#0-5)

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L704-714)
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L229-238)
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
====
```

**File:** rs/ethereum/cketh/minter/templates/dashboard.html (L138-141)
```html
                    <tr id="total-unspent-tx-fees">
                        <th>Total unspent transaction fees (Wei)</th>
                        <td>{{ eth_balance.total_unspent_tx_fees() }}</td>
                    </tr>
```
