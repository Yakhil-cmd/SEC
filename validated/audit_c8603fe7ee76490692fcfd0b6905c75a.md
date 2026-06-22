### Title
Unspent Transaction Fees Permanently Retained by ckETH Minter Without Minting Back to Users — (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary
The ckETH minter charges users a conservatively estimated maximum transaction fee (`max_tx_fee_estimate`) at withdrawal time, but only spends the actual on-chain fee (`actual_tx_fee`). The difference — `unspent_tx_fee = max_tx_fee_estimate − actual_tx_fee` — is tracked in `total_unspent_tx_fees` but is **never minted back as ckETH to any recipient**. This is structurally identical to the external report: a fee is deducted from one party (the withdrawing user, via ckETH burn) but never credited to any party in the ckETH ledger, causing a permanent, growing imbalance between ckETH supply and ETH backing.

### Finding Description

In `update_balance_upon_withdrawal()`, when a withdrawal transaction is finalized on Ethereum:

1. `charged_tx_fee` is computed as the full amount deducted from the user's ckETH burn (for ckETH withdrawals: `withdrawal_amount − tx.amount`; for ckERC20: `req.max_transaction_fee`).
2. `unspent_tx_fee = charged_tx_fee − actual_tx_fee` is calculated.
3. `total_unspent_tx_fees_add(unspent_tx_fee)` is called — this is a **metrics-only** accumulator.
4. **No ckETH is minted back** to the user or any fee recipient for the unspent portion. [1](#0-0) 

For successful ckETH withdrawals, `record_finalized_transaction` does **not** create a reimbursement request for the unspent fee: [2](#0-1) 

For ckERC20 withdrawals, the documentation explicitly confirms this is the intended behavior: [3](#0-2) 

The `EthBalance` struct tracks `total_unspent_tx_fees` as a pure accounting metric with no corresponding ckETH minting path: [4](#0-3) 

The ckETH documentation itself acknowledges the systematic overcharge: [5](#0-4) 

### Impact Explanation

Every successful ckETH or ckERC20 withdrawal permanently deflates the ckETH supply by `unspent_tx_fee`:

- User burns `withdrawal_amount` ckETH.
- User receives `withdrawal_amount − max_tx_fee_estimate` ETH.
- Minter spends only `actual_tx_fee` ETH on-chain.
- `unspent_tx_fee` ETH remains in the minter's Ethereum address with **no corresponding ckETH liability**.

Over time, the cumulative `total_unspent_tx_fees` represents a growing pool of ETH that is permanently locked in the minter's address and unclaimable by any ckETH holder. The ckETH/ETH peg is structurally broken: the minter holds more ETH than the ckETH supply implies, but this surplus is inaccessible and unaccounted for in the ledger. Users are systematically overcharged on every withdrawal.

**Impact: Medium** — No funds are immediately stolen, but the ckETH supply is permanently and cumulatively deflated, and users are overcharged on every withdrawal.

### Likelihood Explanation

This occurs on **every** successful ckETH or ckERC20 withdrawal where `actual_tx_fee < max_tx_fee_estimate`, which is nearly always the case because the minter deliberately sets a conservative upper bound on fees to ensure transaction inclusion even under gas price spikes. Any unprivileged user calling `withdraw_eth` or `withdraw_erc20` triggers this path.

**Likelihood: Medium** — Systematic and predictable; no special conditions required.

### Recommendation

For successful withdrawals, mint back `unspent_tx_fee` as ckETH to the original withdrawer (analogous to how failed ckETH withdrawals already reimburse `withdrawal_amount − actual_tx_fee`). Alternatively, route the unspent fee to a designated fee collector account on the ckETH ledger so it is properly accounted for in the ckETH supply. [6](#0-5) 

### Proof of Concept

1. User calls `withdraw_eth(amount = 1_000_000_000_000_000_000 wei)`.
2. Minter estimates `max_tx_fee_estimate = 1_823_126_598_888_000 wei` (as in the documented example).
3. Minter burns `1_000_000_000_000_000_000` ckETH from user.
4. Minter sends `1_000_000_000_000_000_000 − 1_823_126_598_888_000 = 998_176_873_401_112_000 wei` ETH to destination.
5. Actual on-chain fee: `actual_tx_fee = 899_399_014_248_000 wei`.
6. `unspent_tx_fee = 1_823_126_598_888_000 − 899_399_014_248_000 = 923_727_584_640_000 wei`.
7. `total_unspent_tx_fees` increases by `923_727_584_640_000 wei`.
8. **No ckETH is minted back.** The ckETH supply is permanently deflated by `923_727_584_640_000 wei` (~0.00092 ETH) for this single withdrawal. [7](#0-6) 

Across all historical withdrawals, the minter's `total_unspent_tx_fees` metric represents the total ETH permanently retained without ckETH backing: [8](#0-7)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1575-1603)
```rust
        fn should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = ckerc20_withdrawal_request_with_index(
                cketh_ledger_burn_index,
                ckerc20_ledger_burn_index,
            );
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(40_000_u32),
                effective_gas_price: WeiPerGas::from(100_u16),
                ..transaction_receipt(&signed_tx, TransactionStatus::Success)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                Wei::from(4_000_000_u32)
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(transactions.reimbursement_requests, btreemap! {});
        }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-141)
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
        let reimbursed = Reimbursed {
            burn_in_block: reimbursement_request.ledger_burn_index,
            reimbursed_in_block: LedgerMintIndex::new(block_index),
            reimbursed_amount: reimbursement_request.reimbursed_amount,
            transaction_hash: reimbursement_request.transaction_hash,
        };
        let event = match index {
            ReimbursementIndex::CkEth {
                ledger_burn_index: _,
            } => EventType::ReimbursedEthWithdrawal(reimbursed),
            ReimbursementIndex::CkErc20 {
                cketh_ledger_burn_index,
                ledger_id,
                ckerc20_ledger_burn_index: _,
            } => EventType::ReimbursedErc20Withdrawal {
                cketh_ledger_burn_index,
                ckerc20_ledger_id: ledger_id,
                reimbursed,
            },
        };
        mutate_state(|s| process_event(s, event));
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
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
