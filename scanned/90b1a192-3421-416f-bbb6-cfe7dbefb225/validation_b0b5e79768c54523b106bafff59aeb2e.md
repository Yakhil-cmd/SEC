### Title
ckERC20 Minter Ledger Conservation Bug: Rebasable ERC-20 Token Supply Changes Cause Permanent ckERC20 Insolvency - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary

The ckETH minter tracks ERC-20 token balances held at its Ethereum address purely from audit events (deposits and withdrawals), with no mechanism to reconcile against the actual on-chain balance. If a rebasable ERC-20 token (one whose supply adjusts automatically, e.g., stETH, AMPL) is added as a supported ckERC20 token, a negative rebase causes the minter's actual ERC-20 holdings to shrink while the ckERC20 total supply on the IC remains unchanged. The result is a permanent insolvency: ckERC20 tokens become unredeemable, and users lose ckETH gas fees on every failed withdrawal attempt.

### Finding Description

The `State` struct in `rs/ethereum/cketh/minter/src/state.rs` maintains an `Erc20Balances` field that is explicitly documented as "Computed based on audit events": [1](#0-0) 

The `Erc20Balances` struct stores a simple `BTreeMap<Address, Erc20Value>` and is updated only in two places:

1. **On deposit** (`update_balance_upon_deposit`): adds the deposited amount to the internal map. [2](#0-1) 

2. **On successful withdrawal finalization** (`update_balance_upon_withdrawal`): subtracts the withdrawn amount, but **only when `receipt.status == TransactionStatus::Success`**. [3](#0-2) 

When a user deposits a rebasable ERC-20 token, the minter mints ckERC20 1:1 with the event's `value` field: [4](#0-3) 

After a **negative rebase** (supply contraction), the minter's actual ERC-20 balance at its Ethereum address decreases, but:
- The ckERC20 ledger total supply is unchanged.
- The minter's internal `erc20_balances` is unchanged.
- There is no timer task, reconciliation loop, or on-chain balance query that detects or corrects this divergence.

When a user subsequently calls `withdraw_erc20`, the minter burns their ckERC20 tokens and constructs an Ethereum ERC-20 `transfer` transaction for the full recorded amount. Because the minter's actual ERC-20 balance is now less than the withdrawal amount, the Ethereum transaction reverts. The minter detects the failure via the transaction receipt and reimburses the ckERC20 tokens via `process_reimbursement`: [5](#0-4) 

However, the ckETH gas fee burned for the failed transaction is **not** fully reimbursed (a penalty is applied): [6](#0-5) 

The ckERC20 tokens are re-minted to the user, but the underlying ERC-20 tokens are still insufficient. Every subsequent withdrawal attempt repeats this cycle: burn ckERC20 → Ethereum tx fails → reimburse ckERC20 (minus ckETH gas fee). The ckERC20 tokens are permanently unredeemable.

### Impact Explanation

- **Ledger conservation break**: After a negative rebase, `ckERC20 total supply > actual ERC-20 held by minter`. The invariant that every ckERC20 token is backed 1:1 by an ERC-20 token at the minter's Ethereum address is permanently violated.
- **User fund loss**: Users holding ckERC20 of a rebased token cannot redeem them for the underlying ERC-20. Each failed withdrawal attempt also drains a small amount of ckETH (gas fee penalty).
- **Systemic insolvency**: The last users to attempt withdrawal receive nothing, as the minter's ERC-20 balance is exhausted by earlier (partial) successful withdrawals if the rebase is partial, or all withdrawals fail if the rebase is total.

### Likelihood Explanation

The ckERC20 system is explicitly designed to support any ERC-20 token added via NNS proposal. Rebasable tokens (stETH, AMPL, OHM, etc.) are prominent and widely used ERC-20 tokens. There is no on-chain or off-chain filter in the minter that prevents a rebasable token from being added. Once added, a rebase is an autonomous, protocol-level event on Ethereum that requires no attacker action. Any holder of ckERC20 tokens for a rebased token is affected automatically.

### Recommendation

1. **Reject rebasable tokens at the governance level**: Document and enforce that only non-rebasable ERC-20 tokens (fixed-balance tokens) may be added as supported ckERC20 tokens.
2. **Periodic balance reconciliation**: Add a timer task that queries the actual ERC-20 balance at the minter's Ethereum address via `eth_call` (using the EVM RPC canister) and compares it against the internal `erc20_balances`. If a discrepancy is detected, halt new withdrawals for that token and alert operators.
3. **Share-based accounting**: Instead of tracking absolute token amounts, track each depositor's proportional share of the minter's total ERC-20 holdings (analogous to the recommendation in the original LOB report). Withdrawal amounts would then be computed as `user_share * actual_balance / total_shares`.

### Proof of Concept

1. NNS adds AMPL (a rebasable ERC-20) as a supported ckERC20 token.
2. Alice deposits 1,000 AMPL via the helper contract. The minter observes the `ReceivedEthOrErc20` log event with `value = 1000`, calls `erc20_add(AMPL, 1000)`, and mints 1,000 ckAMPL to Alice on the IC. [2](#0-1) 
3. AMPL undergoes a 50% negative rebase. The minter's actual AMPL balance at its Ethereum address drops to 500. The IC-side `erc20_balances[AMPL]` remains 1,000. The ckAMPL ledger total supply remains 1,000.
4. Alice calls `withdraw_erc20(amount=1000, ckerc20_ledger_id=ckAMPL_ledger, recipient=eth_addr)`. The minter burns 1,000 ckAMPL from Alice's account and constructs an Ethereum transaction calling `transfer(eth_addr, 1000)` on the AMPL contract.
5. The Ethereum transaction reverts because the minter only holds 500 AMPL. The minter reads the failed receipt (`TransactionStatus::Failure`), skips `erc20_sub` (condition not met at line 377), and schedules a reimbursement of 1,000 ckAMPL. [7](#0-6) 
6. `process_reimbursement` mints 1,000 ckAMPL back to Alice. Alice's ckETH balance is reduced by the gas fee penalty. Alice is back to 1,000 ckAMPL but still cannot redeem them. The cycle repeats indefinitely. [8](#0-7) 
7. Bob deposits 500 AMPL after the rebase. The minter mints 500 ckAMPL to Bob. Now ckAMPL total supply = 1,500 but actual AMPL held = 1,000. Bob can withdraw his 500 AMPL successfully, but Alice's 1,000 ckAMPL remain permanently unredeemable.

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L74-76)
```rust
    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-338)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L365-383)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-82)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L507-514)
```rust
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
```
