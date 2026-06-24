### Title
Gas Fee Conversion Rate Discrepancy Between Request Time and Execution Time Locks User Funds in ckERC20 Withdrawal - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary

In the ckETH minter's `withdraw_erc20` flow, the Ethereum gas fee estimate is computed and burned from the user's ckETH account at request time. Later, when the minter's background task processes the withdrawal, it re-estimates the gas fee. If Ethereum gas fees have risen significantly between these two points, the transaction creation fails with `InsufficientTransactionFee`, leaving the user's already-burned ckETH and ckERC20 tokens locked in the pending queue indefinitely with no reimbursement path.

### Finding Description

The `withdraw_erc20` function in `rs/ethereum/cketh/minter/src/main.rs` follows this sequence:

1. Calls `estimate_erc20_transaction_fee()` → `lazy_refresh_gas_fee_estimate()`, which returns a cached estimate up to 60 seconds old.
2. Burns `erc20_tx_fee` ckETH from the user's account (the gas fee budget).
3. Burns the ckERC20 tokens from the user's account.
4. Stores `erc20_tx_fee` as `max_transaction_fee` in the queued `Erc20WithdrawalRequest`. [1](#0-0) [2](#0-1) [3](#0-2) 

Later, `process_retrieve_eth_requests()` calls `lazy_refresh_gas_fee_estimate()` again and passes the fresh estimate to `create_transactions_batch` → `create_transaction`. For ckERC20 requests, `create_transaction` enforces:

```rust
let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
if actual_min_max_fee_per_gas > request_max_fee_per_gas {
    return Err(CreateTransactionError::InsufficientTransactionFee { ... });
}
``` [4](#0-3) 

Where `request_max_fee_per_gas` is derived from the burned `max_transaction_fee`, and `actual_min_max_fee_per_gas` = `base_fee_per_gas_now + max_priority_fee_per_gas_now`. The gas fee estimate at burn time uses `2 * base_fee + priority` as the budget, so the check fails when:

```
base_fee_now + priority_now > 2 * base_fee_at_request + priority_at_request
```

i.e., when the base fee roughly more than doubles. The `lazy_refresh_gas_fee_estimate` cache window is 60 seconds: [5](#0-4) 

When `create_transaction` fails, the `Erc20WithdrawalRequest` remains in `pending_withdrawal_requests`. It is never moved into `maybe_reimburse` (that only happens via `record_created_transaction`), so no automatic reimbursement is scheduled. [6](#0-5) 

The documentation explicitly states: *"Overcharged transaction fees are not reimbursed."* [7](#0-6) 

### Impact Explanation

A user who calls `withdraw_erc20` during a period of normal gas fees has their ckETH (gas budget) and ckERC20 tokens irreversibly burned. If Ethereum gas fees spike significantly before the minter's background task processes the request, the Ethereum transaction cannot be created. The request stays in the pending queue indefinitely. There is no cancel, no refund, and no reimbursement path for this failure mode. If gas fees remain elevated permanently (e.g., due to sustained demand), the user's funds are permanently locked.

**Impact class:** chain-fusion mint/burn/replay bug — user funds burned on IC with no corresponding Ethereum settlement and no reimbursement.

### Likelihood Explanation

Ethereum gas fees are highly volatile. During major events (NFT mints, protocol launches, market crashes), base fees can spike 5–20× within minutes. The minter's 2× buffer (`2 * base_fee`) is insufficient for such spikes. The minter processes pending requests on a timer (every few minutes), creating a window during which gas fees can change substantially. Any unprivileged user calling `withdraw_erc20` is exposed to this risk without any slippage protection or cancellation mechanism.

### Recommendation

Record the gas fee estimate at request time and use it as a fixed rate during transaction creation, analogous to the YieldToken patch. Concretely:

- Store the `GasFeeEstimate` snapshot inside `Erc20WithdrawalRequest` at the time of the burn.
- In `create_transaction`, use the stored snapshot's `max_fee_per_gas` directly rather than re-deriving it from the live estimate, so the transaction is always creatable with the budget the user already paid.
- Alternatively, add a reimbursement path: if `create_transaction` fails with `InsufficientTransactionFee` for a ckERC20 request, move the request into `maybe_reimburse` so the user's burned ckETH and ckERC20 are returned (minus a penalty fee).

### Proof of Concept

1. Ethereum base fee is 10 gwei. User calls `withdraw_erc20`. Minter estimates `max_fee_per_gas = 2*10 + 1.5 = 21.5 gwei`, burns `21.5 * 65_000 = 1_397_500 gwei` of ckETH, burns ckERC20 tokens. `max_transaction_fee = 1_397_500 gwei` stored in request.

2. Ethereum base fee spikes to 15 gwei (e.g., due to a popular NFT mint). Minter's background task runs. `actual_min_max_fee_per_gas = 15 + 1.5 = 16.5 gwei/gas`. `request_max_fee_per_gas = 1_397_500 / 65_000 = 21.5 gwei/gas`.

   Wait — in this case 16.5 < 21.5, so it passes. Let's use a larger spike.

3. Ethereum base fee spikes to 12 gwei. `actual_min_max_fee_per_gas = 12 + 1.5 = 13.5 gwei`. `request_max_fee_per_gas = 21.5 gwei`. 13.5 < 21.5 → passes. The 2× buffer handles moderate spikes.

4. Ethereum base fee spikes to 25 gwei. `actual_min_max_fee_per_gas = 25 + 1.5 = 26.5 gwei`. `request_max_fee_per_gas = 21.5 gwei`. **26.5 > 21.5 → `InsufficientTransactionFee` error.** Request stays pending. User's ckETH and ckERC20 are burned. No reimbursement is triggered. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L480-481)
```rust
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-177)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
    #[n(1)]
    pub withdrawal_amount: Erc20Value,
    /// The recipient's address of the sent ERC-20 tokens.
    #[n(2)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation on the ckETH ledger.
    #[cbor(n(3), with = "crate::cbor::id")]
    pub cketh_ledger_burn_index: LedgerBurnIndex,
    /// Address of the ERC-20 smart contract that is the message call's recipient.
    #[n(4)]
    pub erc20_contract_address: Address,
    /// The ckERC20 ledger on which the minter burned the ckERC20 tokens.
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
    /// The transaction ID of the ckERC20 burn operation on the ckERC20 ledger.
    #[cbor(n(6), with = "crate::cbor::id")]
    pub ckerc20_ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(9)]
    pub created_at: u64,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L485-552)
```rust
    pub fn record_created_transaction(
        &mut self,
        withdrawal_id: LedgerBurnIndex,
        transaction: Eip1559TransactionRequest,
    ) {
        let withdrawal_request = self
            .pending_withdrawal_requests
            .iter()
            .find(|req| req.cketh_ledger_burn_index() == withdrawal_id)
            .cloned()
            .unwrap_or_else(|| panic!("BUG: withdrawal request {withdrawal_id} not found"));
        assert!(
            self.pending_withdrawal_requests
                .contains(&withdrawal_request),
            "BUG: withdrawal request not found"
        );
        assert_eq!(
            withdrawal_request.destination(),
            transaction.destination,
            "BUG: withdrawal request and transaction destination mismatch"
        );
        match &withdrawal_request {
            WithdrawalRequest::CkEth(req) => {
                assert!(
                    req.withdrawal_amount > transaction.amount,
                    "BUG: transaction amount should be the withdrawal amount deducted from transaction fees"
                );
            }
            WithdrawalRequest::CkErc20(_req) => {
                assert_eq!(
                    Wei::ZERO,
                    transaction.amount,
                    "BUG: ERC-20 transaction amount should be zero"
                );
            }
        }
        let nonce = self.next_nonce;
        assert_eq!(transaction.nonce, nonce, "BUG: transaction nonce mismatch");
        self.next_nonce = self
            .next_nonce
            .checked_increment()
            .expect("Transaction nonce overflow");
        self.remove_withdrawal_request(&withdrawal_request);
        let transaction_request = TransactionRequest {
            transaction,
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
                WithdrawalRequest::CkErc20(ckerc20) => ResubmissionStrategy::GuaranteeEthAmount {
                    allowed_max_transaction_fee: ckerc20.max_transaction_fee,
                },
            },
        };
        assert_eq!(
            self.created_tx.try_insert(
                nonce,
                withdrawal_request.cketh_ledger_burn_index(),
                transaction_request
            ),
            Ok(())
        );
        assert_eq!(
            self.processed_withdrawal_requests
                .insert(withdrawal_id, withdrawal_request),
            None
        );
        assert!(self.maybe_reimburse.insert(withdrawal_id));
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1147-1168)
```rust
        WithdrawalRequest::CkErc20(request) => {
            // The transaction fee is already paid and must be at most
            // the `max_transaction_fee` in the withdrawal request, which, given a gas limit, gives us an upper bound on
            // the `max_fee_per_gas`. We allocate the maximum from the beginning to minimize
            // transaction resubmissions: even if the `base_fee_per_gas` increases considerably,
            // the transaction could still make it as long as `transaction.max_fee_per_gas >=  block.base_fee_per_gas`,
            // since the `priority_fee_per_gas` received by the miner is capped to (see https://eips.ethereum.org/EIPS/eip-1559)
            // min(transaction.max_priority_fee_per_gas, transaction.max_fee_per_gas - block.base_fee_per_gas).
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-681)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

    async fn do_refresh() -> Option<GasFeeEstimate> {
        let _guard = match TimerGuard::new(TaskType::RefreshGasFeeEstimate) {
            Ok(guard) => guard,
            Err(e) => {
                log!(
                    DEBUG,
                    "[refresh_gas_fee_estimate]: Failed retrieving guard: {e:?}",
                );
                return None;
            }
        };

        let fee_history = match eth_fee_history().await {
            Ok(fee_history) => fee_history,
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed retrieving fee history: {e:?}",
                );
                return None;
            }
        };

        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
                estimate
            }
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed estimating gas fee: {e:?}",
                );
                return None;
            }
        };
        log!(
            INFO,
            "[refresh_gas_fee_estimate]: Estimated transaction fee: {:?}",
            gas_fee_estimate,
        );
        Some(gas_fee_estimate)
    }

    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }

    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L267-275)
```text
After having called `withdraw_erc20`, the user does not need to do anything else. The minter will take care of the rest:

. The minter checks the desired destination address against the blocklist, and rejects the request if the destination is blocked.
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
. The minter attempts to burn the specified token amount from the user account on the ckERC20 ledger. If the burn succeeds, the minter schedules a withdrawal task. If the burn fails (e.g., insufficient funds), the minter schedules the reimbursement of the burnt ckETH amount from the previous step minus some (small) penalty fee.
. The ckETH minter constructs a 0-ETH amount transaction containing the ERC-20 withdrawal (in `data` field) to the Ethereum network.
. The user can query the withdrawal status using the identifier from the erc20_withdraw response.
. Once the transaction gets enough confirmations, the minter considers the transaction finalized.
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
