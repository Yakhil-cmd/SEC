### Title
Unspent ETH Transaction Fees Accumulate on ckETH Minter's Ethereum Address With No Recovery Mechanism - (`rs/ethereum/cketh/minter/src/state.rs`)

### Summary

The ckETH minter charges users a conservative `max_tx_fee_estimate` when processing ckETH → ETH withdrawals, but only consumes `actual_tx_fee` on Ethereum. The difference (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) permanently accumulates on the minter's Ethereum address. The minter tracks this in `total_unspent_tx_fees` but exposes no mechanism to sweep, redistribute, or burn these funds. For ckERC20 withdrawals, the documentation explicitly states "Overcharged transaction fees are not reimbursed."

### Finding Description

When a user calls `withdraw_eth` or `withdraw_erc20`, the minter burns the full `withdraw_amount` of ckETH from the user's ledger account, then issues an Ethereum transaction with value `withdraw_amount - max_tx_fee_estimate`. The Ethereum network charges only `actual_tx_fee ≤ max_tx_fee_estimate`. The surplus `unspent_tx_fee` remains on the minter's tECDSA-controlled Ethereum address.

The `update_balance_upon_withdrawal` function in `rs/ethereum/cketh/minter/src/state.rs` computes and records this surplus: [1](#0-0) 

The `EthBalance` struct accumulates `total_unspent_tx_fees` indefinitely: [2](#0-1) 

The minter dashboard exposes this metric as a read-only counter: [3](#0-2) 

The ckETH documentation explicitly confirms the behavior and that no reimbursement occurs: [4](#0-3) 

For ckERC20 withdrawals, the documentation is even more explicit: [5](#0-4) 

The minter's public API (`cketh_minter.did`) contains no endpoint to sweep, redistribute, or burn these accumulated unspent fees. The `process_reimbursement` function in `rs/ethereum/cketh/minter/src/withdraw.rs` only handles failed-transaction reimbursements, not overcharged-fee recovery: [6](#0-5) 

### Impact Explanation

ETH value accumulates on the minter's tECDSA-controlled Ethereum address with no on-chain or canister-level mechanism to recover it. The ckETH supply is reduced by the full `withdraw_amount` on every withdrawal, but the minter's ETH balance only decreases by `withdraw_amount - unspent_tx_fee`. Over time, the minter's ETH balance grows relative to the outstanding ckETH supply, creating a permanent, growing discrepancy. These funds cannot be returned to users, burned to restore ledger conservation, or transferred to any treasury without a minter upgrade.

### Likelihood Explanation

Every successful ckETH or ckERC20 withdrawal generates unspent fees. The minter is actively used on mainnet with thousands of withdrawals. The accumulation is continuous and grows monotonically. Any unprivileged user triggering a withdrawal contributes to the locked balance.

### Recommendation

Add a governance-controlled `sweep_unspent_fees` endpoint to the ckETH minter that either:
1. Mints ckETH proportional to `total_unspent_tx_fees` to a designated treasury account, restoring ledger conservation; or
2. Issues an Ethereum transaction from the minter's address to transfer the accumulated unspent ETH to a designated address.

Alternatively, redesign the fee model to charge users only `actual_tx_fee` post-confirmation (with a reimbursement mint), eliminating the accumulation entirely.

### Proof of Concept

1. User calls `withdraw_eth` with `withdraw_amount = 39_998_000_000_000_000` wei (as in the documented example).
2. Minter burns `39_998_000_000_000_000` ckETH from user's ledger account.
3. Minter issues Ethereum transaction with value `39_998_000_000_000_000 - 1_823_126_598_888_000 = 38_174_873_401_112_000` wei.
4. Ethereum network charges `actual_tx_fee = 899_399_014_248_000` wei.
5. `unspent_tx_fee = 1_823_126_598_888_000 - 899_399_014_248_000 = 923_727_584_640_000` wei remains on minter's address.
6. `total_unspent_tx_fees` increments by `923_727_584_640_000` wei.
7. No canister endpoint exists to recover this ETH. Calling any public minter method confirms no sweep function is available. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L362-375)
```rust
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-65)
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
```
