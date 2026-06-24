Audit Report

## Title
ckETH Minter Withdrawal Permanently Stuck When Gas Fees Spike Beyond Withdrawal Amount — (File: `rs/ethereum/cketh/minter/src/tx.rs`)

## Summary
When a ckETH withdrawal transaction fails to be mined and Ethereum gas fees subsequently spike above the original `withdrawal_amount`, `SignedTransactionRequest::resubmit()` returns `ResubmitTransactionError::InsufficientTransactionFee`. The caller `resubmit_transactions_batch` only logs this error with no reimbursement triggered, leaving the user's ckETH permanently burned with no ETH delivered. Additionally, `create_resubmit_transactions` halts processing on the first such error, blocking all higher-nonce pending withdrawals until a governance upgrade resolves the stuck transaction.

## Finding Description

**Root cause — resubmission strategy for ckETH uses `withdrawal_amount` as the fee ceiling:**

In `record_created_transaction`, ckETH withdrawals are assigned `ResubmissionStrategy::ReduceEthAmount { withdrawal_amount }`: [1](#0-0) 

`allowed_max_transaction_fee()` for this variant returns `withdrawal_amount` directly: [2](#0-1) 

**The strict ceiling check in `resubmit()`:**

If the new gas fee estimate produces a `max_transaction_fee` exceeding `withdrawal_amount`, the function returns an error: [3](#0-2) 

**`create_resubmit_transactions` stops at the first error, blocking all higher-nonce withdrawals:** [4](#0-3) 

**`resubmit_transactions_batch` only logs the error — no reimbursement is triggered:** [5](#0-4) 

**Contrast with the finalized-transaction failure path, which does trigger reimbursement:** [6](#0-5) 

**Contrast with `create_transactions_batch`, which reschedules (safe, no burn yet) on the same error:** [7](#0-6) 

The resubmission path has no equivalent safe fallback: the ckETH burn is already irreversible, the original low-fee transaction will not be mined at elevated network fees, and no automatic reimbursement path exists for `ResubmitTransactionError::InsufficientTransactionFee`.

## Impact Explanation

A user's ckETH is burned from the ICRC-1 ledger but the corresponding ETH is never delivered, violating the chain-fusion 1 ckETH ↔ 1 ETH conservation invariant. This constitutes a permanent, irreversible loss of chain-key assets for the affected user. Additionally, because `create_resubmit_transactions` returns early on the first `InsufficientTransactionFee` error, all pending withdrawals with higher nonces are also blocked until the stuck transaction is resolved via a governance canister upgrade, causing a platform-level DoS on the ckETH withdrawal pipeline. This matches the allowed impact: **High — Significant Chain Fusion / ck-token security impact with concrete user and protocol harm**, and potentially **High — Application/platform-level DoS on the ckETH withdrawal pipeline**.

## Likelihood Explanation

No special privileges are required. Any user can call `withdraw_eth` with the minimum amount. The external trigger is an Ethereum gas spike, which is a historically observed condition (300–500 gwei during NFT mints and DeFi events). The minimum ckETH withdrawal amount was recently reduced from 0.03 ETH to 0.005 ETH: [8](#0-7) 

At 0.005 ETH minimum, a gas price of ~238 gwei for a 21,000-gas transfer produces a fee exactly equal to the withdrawal amount, meaning any moderate gas spike renders the resubmission path permanently stuck. The ckBTC minter already experienced a real-world stuck-transaction incident requiring an emergency governance upgrade due to a related fee-estimation issue: [9](#0-8) 

## Recommendation

In `resubmit_transactions_batch`, when `ResubmitTransactionError::InsufficientTransactionFee` is encountered, initiate a reimbursement of the burned ckETH (minus any fees consumed by the original transaction attempt), analogous to the reimbursement path used for finalized-but-failed Ethereum transactions in `record_finalized_transaction`. Specifically, call `record_reimbursement_request` with `reimbursed_amount = withdrawal_amount - effective_fee_of_original_tx` and emit the appropriate `ReimbursedEthWithdrawal` event. Alternatively, allow users to specify a maximum acceptable fee at withdrawal time so the minter can cancel and reimburse if fees exceed the user's tolerance.

## Proof of Concept

1. Alice calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (0.005 ETH). The minter burns 0.005 ckETH from Alice's ledger account.
2. The minter estimates gas at 10 gwei and creates a transaction with `value = 0.005 ETH - 0.00021 ETH = 0.00479 ETH`. The transaction is broadcast to Ethereum.
3. The transaction is not mined (fee estimate was too low or network congestion).
4. Ethereum gas fees spike to 300 gwei (historically observed).
5. On the next timer tick, `resubmit_transactions_batch` calls `create_resubmit_transactions`. For Alice's transaction: `new_tx_price.max_transaction_fee() = 21_000 × 300 gwei = 0.0063 ETH > 0.005 ETH = allowed_max_transaction_fee`.
6. `resubmit()` returns `Err(InsufficientTransactionFee)`. `create_resubmit_transactions` pushes the error and returns early, blocking all higher-nonce withdrawals.
7. `resubmit_transactions_batch` logs `"Failed to resubmit transaction"` and takes no further action.
8. Alice's 0.005 ckETH is permanently burned; she receives no ETH and has no recourse without a governance upgrade. All subsequent pending withdrawals are also blocked.

A deterministic integration test can reproduce this by: (a) recording a ckETH withdrawal request, (b) creating and signing a transaction at low gas, (c) calling `create_resubmit_transactions` with a `GasFeeEstimate` whose `max_transaction_fee` exceeds `withdrawal_amount`, and (d) asserting that no reimbursement request is recorded in `eth_transactions.reimbursement_requests`.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L530-533)
```rust
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L618-631)
```rust
                Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee {
                    allowed_max_transaction_fee,
                    actual_max_transaction_fee,
                }) => {
                    transactions_to_resubmit.push(Err(
                        ResubmitTransactionError::InsufficientTransactionFee {
                            ledger_burn_index: *burn_index,
                            transaction_nonce: *nonce,
                            allowed_max_transaction_fee,
                            max_transaction_fee: actual_max_transaction_fee,
                        },
                    ));
                    return transactions_to_resubmit;
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L719-731)
```rust
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L136-144)
```rust
impl ResubmissionStrategy {
    pub fn allowed_max_transaction_fee(&self) -> Wei {
        match self {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
            ResubmissionStrategy::GuaranteeEthAmount {
                allowed_max_transaction_fee,
            } => *allowed_max_transaction_fee,
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-174)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
        }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L242-244)
```rust
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
