### Title
Unbounded O(n) Linear Scans Over Uncapped `pending_withdrawal_requests` Queue in ckETH Minter Can Halt Withdrawal Processing - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

### Summary

The ckETH minter stores pending withdrawal requests in a `VecDeque<WithdrawalRequest>` with no enforced size cap. Multiple functions perform O(n) linear scans over this queue on every invocation. Any non-anonymous principal holding ckETH can call the public `withdraw_eth` update endpoint to enqueue requests. If the queue grows large enough, the per-message instruction budget of the minter canister is exhausted during timer-driven batch processing, permanently stalling all ckETH withdrawal processing until the queue drains — which it cannot do if the processing itself traps.

### Finding Description

**Root cause — no queue size cap and O(n) operations on every mutation:**

`pending_withdrawal_requests` is declared as `VecDeque<WithdrawalRequest>` with no upper-bound enforcement. [1](#0-0) 

Every call to `record_withdrawal_request` performs a full O(n) `.iter().any()` duplicate check over the entire queue before appending: [2](#0-1) 

`remove_withdrawal_request` uses `.retain()`, which is O(n): [3](#0-2) 

`reschedule_withdrawal_request` chains an O(n) `.filter().count()` assertion, then calls `remove_withdrawal_request` (O(n) retain), then calls `record_withdrawal_request` (O(n) any) — three full passes per rescheduled request: [4](#0-3) 

`record_created_transaction` performs an O(n) `.iter().find()` followed by an O(n) `.retain()` via `remove_withdrawal_request`: [5](#0-4) 

`transaction_status` (called from the public `retrieve_eth_status` update endpoint) does an O(n) `.iter().any()`: [6](#0-5) 

`withdrawal_status` (called from the public `withdrawal_status` query endpoint) does an O(n) `.iter().filter_map()`: [7](#0-6) 

**Attacker-controlled entry path:**

`withdraw_eth` is a public `#[update]` endpoint callable by any non-anonymous principal. The only per-caller protection is a concurrency guard that prevents two simultaneous calls from the *same* caller; it does not limit the total queue depth across all callers, and it is released after each call completes, allowing the same caller to submit sequentially: [8](#0-7) 

**Contrast with ckBTC minter:** The ckBTC minter explicitly rejects new requests when the pending queue exceeds `MAX_CONCURRENT_PENDING_REQUESTS`: [9](#0-8) 

No equivalent guard exists in the ckETH minter.

**Critical timer path:** The timer-driven `create_transactions_batch` calls `withdrawal_requests_batch` (O(n) iteration) and, for each request with insufficient fees, calls `reschedule_withdrawal_request` (O(3n)). For a batch of size B over a queue of size N, the timer executes O(3·B·N) operations per tick: [10](#0-9) 

### Impact Explanation

If `pending_withdrawal_requests` grows to a size where the cumulative instruction cost of O(n) operations within a single timer message exceeds the IC per-message instruction limit (~5 billion instructions), the timer traps. Because the timer is the sole mechanism that advances withdrawal requests from `Pending` → `TxCreated` → `TxSent` → `TxFinalized`, a trapped timer permanently halts all ckETH withdrawal processing. Users who have already burned ckETH (irreversible on the ledger) cannot receive their ETH. The minter's state is not corrupted, but the service is unavailable until the queue drains — which cannot happen if processing itself traps.

Secondary impact: `retrieve_eth_status` (an `#[update]` call) and `withdrawal_status` (a query) both perform O(n) scans and will also degrade or trap under a large queue.

### Likelihood Explanation

Each withdrawal request requires burning a minimum of `30_000_000_000_000_000` wei (0.03 ETH) of ckETH. The attacker's principal is not permanently lost — the minter eventually sends ETH to the destination — but the capital must remain locked in the queue until processed. The cost per request is the Ethereum gas fee (~$5–50) plus IC fees, not the full 0.03 ETH principal. A well-funded attacker with multiple principals can submit requests sequentially (the per-caller guard releases after each call). Organic growth from many legitimate users during high-demand periods can also inflate the queue without any malicious intent. Likelihood is **low-to-medium**: economically constrained but not impossible, and the design gap (absent size cap) is a latent risk that grows with protocol adoption.

### Recommendation

1. **Enforce a maximum queue depth** in `withdraw_eth`, analogous to ckBTC's `MAX_CONCURRENT_PENDING_REQUESTS` check, rejecting new requests with `TemporarilyUnavailable` when the pending queue exceeds a safe bound (e.g., 1,000–10,000 entries).
2. **Replace the `VecDeque` with a `BTreeMap<LedgerBurnIndex, WithdrawalRequest>`** so that duplicate checks, lookups, and removals are O(log n) instead of O(n).
3. **Remove the O(n) duplicate check** in `record_withdrawal_request`; uniqueness of `LedgerBurnIndex` is already guaranteed by the ledger burn mechanism — the panic guard is redundant and expensive.
4. **Replace the O(n) `.retain()`** in `remove_withdrawal_request` with an O(log n) map removal once the data structure is changed.

### Proof of Concept

1. Attacker holds ckETH (obtained legitimately via deposit).
2. Attacker calls `withdraw_eth` repeatedly from multiple principals (or sequentially from one), each time specifying the minimum withdrawal amount and a controlled Ethereum destination address. Each call succeeds, appending one entry to `pending_withdrawal_requests`.
3. After N requests, the minter's timer fires and calls `create_transactions_batch`. For each of the B requests in the batch that fail fee estimation (e.g., during high gas periods), `reschedule_withdrawal_request` is called, executing O(3N) instructions. Total timer cost: O(3·B·N).
4. At sufficiently large N (exact threshold depends on IC instruction costs per element, estimated at 50–200 instructions per `WithdrawalRequest` comparison), the timer message exceeds the instruction budget and traps.
5. The timer is not rescheduled on trap. All pending withdrawal requests are frozen. Users who burned ckETH cannot receive ETH.
6. The attacker can eventually recover their ETH once the queue is manually drained (requires governance intervention or a minter upgrade), but all other users' withdrawals are blocked in the interim.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L398-411)
```rust
impl EthTransactions {
    pub fn new(next_nonce: TransactionNonce) -> Self {
        Self {
            pending_withdrawal_requests: VecDeque::new(),
            processed_withdrawal_requests: BTreeMap::new(),
            created_tx: MultiKeyMap::default(),
            sent_tx: MultiKeyMap::default(),
            finalized_tx: MultiKeyMap::default(),
            next_nonce,
            maybe_reimburse: Default::default(),
            reimbursement_requests: Default::default(),
            reimbursed: Default::default(),
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L453-467)
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
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L469-483)
```rust
    /// Move an existing withdrawal request to the back of the queue.
    pub fn reschedule_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        assert_eq!(
            self.pending_withdrawal_requests
                .iter()
                .filter(|r| r.cketh_ledger_burn_index() == request.cketh_ledger_burn_index())
                .count(),
            1,
            "BUG: expected exactly one withdrawal request with ckETH ledger burn index {}",
            request.cketh_ledger_burn_index()
        );
        self.remove_withdrawal_request(&request);
        self.record_withdrawal_request(request);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L490-527)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L813-817)
```rust
        // Pending requests matching the given search parameter
        let pending = self.pending_withdrawal_requests.iter().filter_map(|r| {
            r.match_parameter(parameter)
                .then_some((r, WithdrawalStatus::Pending, None))
        });
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L844-852)
```rust
    pub fn transaction_status(&self, burn_index: &LedgerBurnIndex) -> RetrieveEthStatus {
        if self
            .pending_withdrawal_requests
            .iter()
            .any(|r| &r.cketh_ledger_burn_index() == burn_index)
        {
            return RetrieveEthStatus::Pending;
        }
        self.processed_transaction_status(burn_index).0
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1038-1040)
```rust
    fn remove_withdrawal_request(&mut self, request: &WithdrawalRequest) {
        self.pending_withdrawal_requests.retain(|r| r != request);
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L274-279)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
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
