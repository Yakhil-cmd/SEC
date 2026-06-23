### Title
Missing Audit Event for `reschedule_withdrawal_request` State Mutation - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary
The ckETH minter uses an event-sourced architecture where every state mutation must be recorded via `process_event()` so the state can be faithfully reconstructed from the audit log on upgrade. One state mutation — `reschedule_withdrawal_request` — bypasses this mechanism entirely, creating an incomplete audit log and a state divergence after canister upgrades.

### Finding Description
The ckETH minter's design invariant is that all state changes flow through `process_event()`, which atomically applies the state transition and appends the event to the stable `EVENTS` log: [1](#0-0) 

On upgrade, `post_upgrade` calls `replay_events()` to reconstruct the full minter state from this log: [2](#0-1) 

However, in `create_transactions_batch`, when a withdrawal request cannot be processed because the current gas fee exceeds the user's allowed maximum, the request is moved to the back of the queue via a bare `mutate_state` call — with **no corresponding `process_event` call**: [3](#0-2) 

This is the only place in the entire ckETH minter where `mutate_state` is called without a paired `process_event`. Every other state mutation — minting, burning, signing, finalizing, reimbursing — goes through `process_event`. [4](#0-3) 

### Impact Explanation
There are two concrete impacts:

1. **Audit log incompleteness**: External observers calling `get_events` see no record of the reschedule. The audit log, which is the canonical source of truth for the minter's history, silently omits this state transition. This breaks the transparency guarantee of the chain-fusion bridge.

2. **State divergence after upgrade**: Because `replay_events()` reconstructs state solely from the event log, any withdrawal request that was rescheduled (moved to the back of the queue) before an upgrade will reappear at its **original queue position** after the upgrade. This means the same request that was already determined to have insufficient fees will be immediately retried at the front of the queue, hitting the same error again, and again being rescheduled without an event — creating a silent, unrecorded loop. The `check_audit_log` debug function explicitly verifies that replaying events produces an equivalent state; this check would fail whenever a reschedule has occurred: [5](#0-4) 

### Likelihood Explanation
The trigger condition — a withdrawal request whose allowed maximum transaction fee is exceeded by the current gas fee estimate — is a realistic and recurring scenario. Gas prices on Ethereum fluctuate continuously. A user who submits a withdrawal when gas is low may find their request rescheduled when gas spikes. This is not a rare edge case; it is an expected operational condition explicitly handled in the code with a log message. Any canister upgrade performed while rescheduled requests exist in the queue will produce a diverged state.

### Recommendation
Add a new `EventType` variant (e.g., `RescheduledWithdrawalRequest`) and replace the bare `mutate_state` call with a `process_event` call, exactly as is done for every other state mutation in the minter. The `apply_state_transition` match arm for this new variant should call `reschedule_withdrawal_request` on the state.

### Proof of Concept

1. User calls `withdraw_eth` with an amount sufficient to cover fees at current gas prices. An `AcceptedEthWithdrawalRequest` event is recorded.
2. Gas prices spike. The periodic timer fires `process_retrieve_eth_requests` → `create_transactions_batch`.
3. `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`.
4. The code executes `mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request))` — **no event is recorded**.
5. An operator upgrades the ckETH minter canister. `post_upgrade` calls `replay_events()`.
6. The reconstructed state places the withdrawal request back at its original queue position (not the back), because the reschedule was never recorded.
7. `create_transactions_batch` immediately retries the same request, hits the same error, and again silently rescheduled without an event.
8. Calling `check_audit_log` at any point after step 4 would reveal the state divergence, as `replay_events().is_equivalent_to(s)` would fail on the queue ordering. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L171-175)
```rust
/// Records the given event payload in the event log and updates the state to reflect the change.
pub fn process_event(state: &mut State, payload: EventType) {
    apply_state_transition(state, &payload);
    record_event(payload);
}
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L35-43)
```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();

    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());
    });
    if let Some(args) = upgrade_args {
        mutate_state(|s| process_event(s, EventType::Upgrade(args)))
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

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L15-174)
```rust
/// The event describing the ckETH minter state transition.
#[derive(Clone, Eq, PartialEq, Debug, Decode, Encode)]
pub enum EventType {
    /// The minter initialization event.
    /// Must be the first event in the log.
    #[n(0)]
    Init(#[n(0)] InitArg),
    /// The minter upgraded with the specified arguments.
    #[n(1)]
    Upgrade(#[n(0)] UpgradeArg),
    /// The minter discovered a ckETH deposit in the helper contract logs.
    #[n(2)]
    AcceptedDeposit(#[n(0)] ReceivedEthEvent),
    /// The minter discovered an invalid ckETH deposit in the helper contract logs.
    #[n(4)]
    InvalidDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The reason why minter considers the deposit invalid.
        #[n(1)]
        reason: String,
    },
    /// The minter minted ckETH in response to a deposit.
    #[n(5)]
    MintedCkEth {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The transaction index on the ckETH ledger.
        #[cbor(n(1), with = "crate::cbor::id")]
        mint_block_index: LedgerMintIndex,
    },
    /// The minter processed the helper smart contract logs up to the specified height.
    #[n(6)]
    SyncedToBlock {
        /// The last processed block number for ETH helper contract (inclusive).
        #[n(0)]
        block_number: BlockNumber,
    },
    /// The minter accepted a new ETH withdrawal request.
    #[n(7)]
    AcceptedEthWithdrawalRequest(#[n(0)] EthWithdrawalRequest),
    /// The minter created a new transaction to handle a withdrawal request.
    #[n(8)]
    CreatedTransaction {
        #[cbor(n(0), with = "crate::cbor::id")]
        withdrawal_id: LedgerBurnIndex,
        #[n(1)]
        transaction: Eip1559TransactionRequest,
    },
    /// The minter signed a transaction.
    #[n(9)]
    SignedTransaction {
        /// The withdrawal identifier.
        #[cbor(n(0), with = "crate::cbor::id")]
        withdrawal_id: LedgerBurnIndex,
        /// The signed transaction.
        #[n(1)]
        transaction: SignedEip1559TransactionRequest,
    },
    /// The minter created a new transaction to handle an existing withdrawal request.
    #[n(10)]
    ReplacedTransaction {
        /// The withdrawal identifier.
        #[cbor(n(0), with = "crate::cbor::id")]
        withdrawal_id: LedgerBurnIndex,
        /// The replacement transaction.
        #[n(1)]
        transaction: Eip1559TransactionRequest,
    },
    /// The minter observed the transaction being included in a finalized Ethereum block.
    #[n(11)]
    FinalizedTransaction {
        /// The withdrawal identifier.
        #[cbor(n(0), with = "crate::cbor::id")]
        withdrawal_id: LedgerBurnIndex,
        /// The receipt for the finalized transaction.
        #[n(1)]
        transaction_receipt: TransactionReceipt,
    },
    /// The minter successfully reimbursed a failed withdrawal
    /// or the transaction fee associated with a ckERC20 withdrawal.
    #[n(12)]
    ReimbursedEthWithdrawal(#[n(0)] Reimbursed),
    /// Add a new ckERC20 token.
    #[n(14)]
    AddedCkErc20Token(#[n(0)] CkErc20Token),
    /// The minter discovered a ckERC20 deposit in the helper contract logs.
    #[n(15)]
    AcceptedErc20Deposit(#[n(0)] ReceivedErc20Event),
    /// The minter accepted a new ERC-20 withdrawal request.
    #[n(16)]
    AcceptedErc20WithdrawalRequest(#[n(0)] Erc20WithdrawalRequest),
    #[n(17)]
    MintedCkErc20 {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The transaction index on the ckETH ledger.
        #[cbor(n(1), with = "crate::cbor::id")]
        mint_block_index: LedgerMintIndex,
        #[n(2)]
        ckerc20_token_symbol: String,
        #[n(3)]
        erc20_contract_address: Address,
    },
    /// The minter processed the helper smart contract logs up to the specified height.
    #[n(18)]
    SyncedErc20ToBlock {
        /// The last processed block number for ERC20 helper contract (inclusive).
        #[n(0)]
        block_number: BlockNumber,
    },
    #[n(19)]
    ReimbursedErc20Withdrawal {
        #[cbor(n(0), with = "crate::cbor::id")]
        cketh_ledger_burn_index: LedgerBurnIndex,
        #[cbor(n(1), with = "icrc_cbor::principal")]
        ckerc20_ledger_id: Principal,
        #[n(2)]
        reimbursed: Reimbursed,
    },
    /// The minter could not burn the given amount of ckERC20 tokens.
    #[n(20)]
    FailedErc20WithdrawalRequest(#[n(0)] ReimbursementRequest),
    /// The minter unexpectedly panic while processing a deposit.
    /// The deposit is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(21)]
    QuarantinedDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
    },
    /// The minter unexpectedly panic while processing a reimbursement.
    /// The reimbursement is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(22)]
    QuarantinedReimbursement {
        /// The unique identifier of the reimbursement.
        #[n(0)]
        index: ReimbursementIndex,
    },
    /// Skipped block for a specific helper contract.
    #[n(23)]
    SkippedBlockForContract {
        #[n(0)]
        contract_address: Address,
        #[n(1)]
        block_number: BlockNumber,
    },
    /// The minter processed the deposit helper smart contract with subaccount logs up to the specified height.
    #[n(24)]
    SyncedDepositWithSubaccountToBlock {
        /// The last processed block number for the helper contract (inclusive).
        #[n(0)]
        block_number: BlockNumber,
    },
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L1119-1131)
```rust
#[cfg(feature = "debug_checks")]
#[query]
fn check_audit_log() {
    use ic_cketh_minter::state::audit::replay_events;

    emit_preupgrade_events();

    read_state(|s| {
        replay_events()
            .is_equivalent_to(s)
            .expect("replaying the audit log should produce an equivalent state")
    })
}
```

**File:** rs/ethereum/cketh/minter/src/storage.rs (L49-59)
```rust
/// Appends the event to the event log.
pub fn record_event(payload: EventType) {
    EVENTS
        .with(|events| {
            events.borrow().append(&Event {
                timestamp: ic_cdk::api::time(),
                payload,
            })
        })
        .expect("recording an event should succeed");
}
```
