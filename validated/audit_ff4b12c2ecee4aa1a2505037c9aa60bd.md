### Title
Unbounded Pending Withdrawal Queue and Global Nonce Cap Enable Forced Halt of All ckETH/ckERC20 Withdrawal Processing - (File: `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The ckETH minter's `EthTransactions` state machine contains two compounding weaknesses that allow an unprivileged caller to forcibly stall all withdrawal processing for every user of the ckETH/ckERC20 bridge. First, `pending_withdrawal_requests` is an unbounded `VecDeque` with no admission cap. Second, `withdrawal_requests_batch` enforces a hard global ceiling of `MAX_NUM_PENDING_TRANSACTION_NONCES = 1000` across `created_tx` and `sent_tx`; once that ceiling is reached the function returns an empty batch, and the minter's timer loop stops converting any pending request into an Ethereum transaction until the backlog drains. An attacker who can afford the gas cost of ~1 000 Ethereum transactions can hold the entire withdrawal pipeline in a frozen state for as long as Ethereum congestion keeps those transactions unconfirmed.

---

### Finding Description

`withdrawal_requests_batch` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` computes the number of unique nonces already in flight and subtracts from 1 000 to derive the batch size it will return:

```rust
const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
let unique_pending_transaction_nonces: BTreeSet<_> =
    self.created_tx.keys().chain(self.sent_tx.keys()).collect();
let actual_batch_size = min(
    MAX_NUM_PENDING_TRANSACTION_NONCES
        .saturating_sub(unique_pending_transaction_nonces.len()),
    requested_batch_size,
);
``` [1](#0-0) 

When `unique_pending_transaction_nonces.len() >= 1000`, `actual_batch_size` saturates to 0 and the function returns an empty `Vec`. The caller in `withdraw.rs` iterates over this empty result and does nothing:

```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) { ... }
}
``` [2](#0-1) 

The batch size constant is only 5: [3](#0-2) 

The queue that feeds this pipeline, `pending_withdrawal_requests`, is a plain `VecDeque` with no size bound: [4](#0-3) 

`record_withdrawal_request` appends unconditionally after a duplicate-index check: [5](#0-4) 

The public entry points `withdraw_eth` and `withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs` accept any non-anonymous caller who can burn the minimum ckETH amount; there is no per-principal or global cap on the number of accepted-but-unprocessed requests: [6](#0-5) [7](#0-6) 

The per-caller guard (`retrieve_withdraw_guard`) only prevents two *concurrent* calls from the same principal; it is released as soon as the async burn completes, so the same principal can submit requests sequentially without restriction.

---

### Impact Explanation

Once an attacker drives `created_tx ∪ sent_tx` to 1 000 unique nonces, `withdrawal_requests_batch` returns an empty slice on every timer tick. The minter's `create_transactions_batch` loop processes zero requests. Every legitimate user whose withdrawal request is already in `pending_withdrawal_requests` remains stuck there indefinitely. No new Ethereum transactions are created for any user until the attacker's 1 000 transactions finalize on-chain and drain the nonce maps. During Ethereum congestion the attacker can keep resubmitting with low gas to delay finalization, extending the freeze window. The impact is a global, protocol-level denial of service on all ckETH and ckERC20 withdrawals — functionally identical to the bridge's "global withdrawal queue activation" described in the external report.

---

### Likelihood Explanation

The attacker must burn enough ckETH to submit ~1 000 withdrawal requests (each above `cketh_minimum_withdrawal_amount`) and pay Ethereum gas for those transactions. The ckETH principal is not lost — it is converted to ETH at the destination address — so the net cost is only gas fees (~1 000 × current gas price × 21 000 gas for ETH transfers, or ~65 000 gas for ERC-20). During low-fee periods this is economically feasible for a motivated attacker. The attack is amplified during Ethereum congestion because stuck transactions extend the freeze window at no additional cost to the attacker. No privileged role, governance vote, or subnet-majority corruption is required.

---

### Recommendation

**Short term:** Enforce a maximum size on `pending_withdrawal_requests` inside `record_withdrawal_request`. Reject new withdrawal requests with a `TemporarilyUnavailable` error when the queue exceeds a safe threshold (e.g., 2 000–5 000 entries). This bounds the memory growth and limits how far ahead of legitimate users an attacker can queue.

**Long term:** Decouple the global nonce cap from the per-user fairness guarantee. Consider a per-principal request quota so that a single actor cannot monopolise the 1 000-nonce budget. Additionally, implement a priority or age-based eviction policy so that long-queued legitimate requests are not indefinitely starved behind attacker-controlled requests.

---

### Proof of Concept

1. Attacker acquires ≥ 1 000 × `cketh_minimum_withdrawal_amount` of ckETH across N principals (or sequentially from one principal).
2. Attacker calls `withdraw_eth` (or `withdraw_erc20`) repeatedly. Each call burns ckETH and appends to `pending_withdrawal_requests` via `record_withdrawal_request`.
3. The minter's timer runs `create_transactions_batch` with `WITHDRAWAL_REQUESTS_BATCH_SIZE = 5`, processing 5 requests per tick and moving them into `created_tx`.
4. After signing, they move to `sent_tx` and are broadcast to Ethereum.
5. Attacker sets a low `max_priority_fee_per_gas` so transactions sit unconfirmed in the Ethereum mempool.
6. After ~200 timer ticks, `created_tx.keys().chain(sent_tx.keys())` reaches 1 000 unique nonces.
7. `withdrawal_requests_batch` now returns `[]` on every tick:
   ```
   actual_batch_size = min(1000 - 1000, 5) = min(0, 5) = 0
   ```
8. All legitimate users' requests remain in `pending_withdrawal_requests` and are never promoted to `created_tx`. The ckETH/ckERC20 withdrawal bridge is frozen for all users until the attacker's transactions confirm on Ethereum. [8](#0-7) [5](#0-4) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L362-362)
```rust
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L453-466)
```rust
    pub fn record_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        let burn_index = request.cketh_ledger_burn_index();
        if self
            .pending_withdrawal_requests
            .iter()
            .any(|r| r.cketh_ledger_burn_index() == burn_index)
            || self.created_tx.contains_alt(&burn_index)
            || self.sent_tx.contains_alt(&burn_index)
            || self.finalized_tx.contains_alt(&burn_index)
        {
            panic!("BUG: duplicate ckETH ledger burn index {burn_index}");
        }
        self.pending_withdrawal_requests.push_back(request);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L906-923)
```rust
    pub fn withdrawal_requests_batch(&self, requested_batch_size: usize) -> Vec<WithdrawalRequest> {
        // The number of pending transaction nonces is counted and not the number of pending transactions
        // because a nonce may be associated with several distinct transactions (due to re-submission and dynamic fees).
        // However, once a nonce is chosen for a withdrawal request, it's in our interest that the corresponding transaction be finalized asap.
        // Limiting the number of transactions would be counter-productive.
        const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
        let unique_pending_transaction_nonces: BTreeSet<_> =
            self.created_tx.keys().chain(self.sent_tx.keys()).collect();
        let actual_batch_size = min(
            MAX_NUM_PENDING_TRANSACTION_NONCES
                .saturating_sub(unique_pending_transaction_nonces.len()),
            requested_batch_size,
        );
        self.withdrawal_requests_iter()
            .take(actual_batch_size)
            .cloned()
            .collect()
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L39-39)
```rust
const WITHDRAWAL_REQUESTS_BATCH_SIZE: usize = 5;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-293)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        log!(DEBUG, "[create_transactions_batch]: processing {request:?}",);
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
            Ok(transaction) => {
                log!(
                    DEBUG,
                    "[create_transactions_batch]: created transaction {transaction:?}",
                );

                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::CreatedTransaction {
                            withdrawal_id: request.cketh_ledger_burn_index(),
                            transaction,
                        },
                    );
                });
            }
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
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
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
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-448)
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
    validate_ckerc20_active();
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");

    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
    match cketh_ledger
```
