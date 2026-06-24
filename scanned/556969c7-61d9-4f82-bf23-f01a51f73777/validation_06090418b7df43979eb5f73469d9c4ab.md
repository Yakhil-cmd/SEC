### Title
ckETH Withdrawal Requests Permanently Stuck in Pending Queue with No Reimbursement Path When Gas Fees Persistently Exceed Withdrawal Amount - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `create_transactions_batch` function handles `CreateTransactionError::InsufficientTransactionFee` by calling `reschedule_withdrawal_request`, which moves the request to the back of the pending queue indefinitely. There is no timeout, no automatic reimbursement, and no user-facing cancellation mechanism. If Ethereum gas fees persistently exceed a user's withdrawal amount (a realistic scenario for small withdrawals near the minimum threshold), the user's ckETH is permanently burned with no recovery path — the funds are locked in the minter's pending queue forever.

---

### Finding Description

When a user calls `withdraw_eth`, the minter immediately burns the user's ckETH from the ledger and enqueues a `WithdrawalRequest` in `pending_withdrawal_requests`. [1](#0-0) 

The periodic timer `process_retrieve_eth_requests` calls `create_transactions_batch`, which attempts to create an EIP-1559 transaction for each pending request. If the current gas fee estimate exceeds the withdrawal amount, `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`. [2](#0-1) 

The error handler in `create_transactions_batch` responds by calling `reschedule_withdrawal_request`, which moves the request to the **back** of the queue: [3](#0-2) 

`reschedule_withdrawal_request` simply removes and re-appends the request with no state change, no reimbursement trigger, and no expiry tracking: [4](#0-3) 

The `EthTransactions` state machine has no concept of a "failed pending" state — a request stuck in `pending_withdrawal_requests` is never automatically moved to `reimbursement_requests`: [5](#0-4) 

The minter's public interface exposes no endpoint for a user to cancel a pending withdrawal or force reimbursement. The DID file confirms the only withdrawal-related endpoints are `withdraw_eth`, `withdraw_erc20`, `retrieve_eth_status`, and `withdrawal_status` — no cancellation endpoint exists: [6](#0-5) 

The minimum withdrawal amount (`cketh_minimum_withdrawal_amount`) is set at governance level and was recently reduced from 0.03 ETH to 0.005 ETH (~$10). This reduction increases the likelihood that a gas fee spike (e.g., during Ethereum network congestion) causes the gas cost to exceed the withdrawal amount for requests near the minimum: [7](#0-6) 

The reimbursement path only exists for transactions that have been **finalized on Ethereum with a failure receipt** — it does not apply to requests that never leave the pending queue: [8](#0-7) 

---

### Impact Explanation

A user who submits a `withdraw_eth` call with an amount near the minimum threshold has their ckETH burned immediately and irreversibly. If Ethereum gas fees spike above the withdrawal amount and remain elevated, the request cycles through `reschedule_withdrawal_request` indefinitely. The user:

1. Has lost their ckETH from the ledger (burned).
2. Cannot receive ETH on Ethereum (transaction never created).
3. Cannot cancel the request or trigger reimbursement.
4. Has no on-chain recourse — only a governance upgrade can rescue the funds.

This is a **permanent lock of user funds** reachable through normal product flows, with no self-service recovery path. The impact is proportional to the number of users with near-minimum pending withdrawals during a gas spike.

---

### Likelihood Explanation

The scenario is realistic and does not require a malicious actor:

- Ethereum gas fees are volatile. Historical spikes (e.g., during NFT mints, network congestion) have pushed `base_fee_per_gas` to hundreds of gwei, making a 21,000-gas ETH transfer cost well above 0.005 ETH.
- The minimum was recently lowered to 0.005 ETH, shrinking the safety margin. The documentation itself acknowledges the ~10× safety margin assumption: [9](#0-8) 

- Any user who submitted a withdrawal at the minimum amount during a low-fee period and then experiences a fee spike before the minter processes the request is affected.
- No privileged access is required — any unprivileged user calling `withdraw_eth` with a near-minimum amount is a potential victim.

---

### Recommendation

1. **Add a maximum pending duration**: Track `created_at` on `WithdrawalRequest` (already present in `EthWithdrawalRequest`) and after a configurable timeout (e.g., 7 days), automatically move the request to `reimbursement_requests` and mint back the ckETH to the user.

2. **Add a user-callable cancellation endpoint**: Allow the original requester to cancel a `Pending` withdrawal and trigger reimbursement, similar to how ckBTC handles `TooManyInputs` cancellations: [10](#0-9) 

3. **Enforce a stricter minimum withdrawal amount**: Ensure `cketh_minimum_withdrawal_amount` always exceeds the maximum plausible gas cost by a sufficient margin, and re-evaluate the minimum dynamically based on observed gas prices.

---

### Proof of Concept

**Attacker-controlled entry path**: Any unprivileged IC principal.

**Steps**:

1. User calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (current minimum, ~$10).
2. Minter burns 5_000_000_000_000_000 wei from the ckETH ledger. User's ckETH balance is now 0.
3. `EthWithdrawalRequest` is enqueued in `pending_withdrawal_requests`.
4. Ethereum gas fees spike: `base_fee_per_gas = 300 gwei`. Gas cost for 21,000 gas = `300e9 * 21000 = 6_300_000_000_000_000 wei` > `5_000_000_000_000_000 wei`.
5. `create_transactions_batch` calls `create_transaction` → returns `CreateTransactionError::InsufficientTransactionFee`.
6. Handler calls `reschedule_withdrawal_request` → request moved to back of queue.
7. Steps 5–6 repeat on every timer tick (every ~5 minutes) indefinitely.
8. User queries `retrieve_eth_status` → `Pending` forever.
9. User has no endpoint to cancel or recover funds.
10. Funds remain locked until a governance upgrade manually intervenes.

The root cause is at: [3](#0-2) 

with the missing reimbursement path confirmed by the absence of any cancellation logic in: [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-336)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L346-377)
```rust
/// State machine holding Ethereum transactions issued by the minter.
/// Overall the transaction lifecycle is as follows:
/// 1. The user's withdrawal request is enqueued and processed in a FIFO order.
/// 2. A transaction is created by either consuming a withdrawal request
///    (the first time a transaction is created for that nonce and burn index)
///    or re-submitting an already sent transaction for that nonce and burn index.
/// 3. The transaction is signed via threshold ECDSA and recorded by either consuming the
///    previously created transaction or re-submitting an already sent transaction as is.
/// 4. The transaction is sent to Ethereum. There may have been multiple
///    sent transactions for that nonce and burn index in case of resubmissions.
/// 5. For a given nonce (and burn index), at most one sent transaction is finalized.
///    The others sent transactions for that nonce were never mined and can be discarded.
/// 6. If a given transaction fails the minter will reimburse the user who requested the
///    withdrawal with the corresponding amount minus fees.
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
    pub(in crate::state) processed_withdrawal_requests:
        BTreeMap<LedgerBurnIndex, WithdrawalRequest>,
    pub(in crate::state) created_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, TransactionRequest>,
    pub(in crate::state) sent_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, Vec<SignedTransactionRequest>>,
    pub(in crate::state) finalized_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, FinalizedEip1559Transaction>,
    pub(in crate::state) next_nonce: TransactionNonce,

    pub(in crate::state) maybe_reimburse: BTreeSet<LedgerBurnIndex>,
    pub(in crate::state) reimbursement_requests: BTreeMap<ReimbursementIndex, ReimbursementRequest>,
    pub(in crate::state) reimbursed: BTreeMap<ReimbursementIndex, ReimbursedResult>,
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1133)
```rust
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-730)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L3108-3173)
```rust
#[test]
fn should_cancel_and_reimburse_large_withdrawal() {
    let ckbtc = CkBtcSetup::new();
    let user = Principal::from(ckbtc.caller);
    let subaccount: Option<[u8; 32]> = Some([1; 32]);
    let user_account = Account {
        owner: user,
        subaccount,
    };

    // Step 1: deposit enough small UTXOs to exceed the max inputs limit.
    // We need at least max + 1 UTXOs for the withdrawal to trigger TooManyInputs,
    // plus a small buffer so there are leftover UTXOs in the set.
    const MAX_INPUTS: usize = ic_ckbtc_minter::state::DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION;
    const NUM_UTXOS: usize = MAX_INPUTS + 100;
    let deposit_value = 100_000_u64;
    let _deposited_utxos =
        ckbtc.deposit_utxos_with_value(user_account, &[deposit_value; NUM_UTXOS]);
    let balance_after_deposit = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_deposit,
        Nat::from(NUM_UTXOS as u64 * (deposit_value - CHECK_FEE))
    );

    let withdrawal_amount = (MAX_INPUTS as u64 + 1) * deposit_value;
    ckbtc.approve_minter(user, withdrawal_amount, subaccount);
    let balance_before_withdrawal = ckbtc.balance_of(user_account);

    let RetrieveBtcOk { block_index } = ckbtc
        .retrieve_btc_with_approval(
            WITHDRAWAL_ADDRESS.to_string(),
            withdrawal_amount,
            subaccount,
        )
        .expect("retrieve_btc failed");

    let balance_after_withdrawal = ckbtc.balance_of(user_account);
    assert_eq!(
        balance_after_withdrawal,
        balance_before_withdrawal.clone() - Nat::from(withdrawal_amount)
    );

    assert_eq!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Pending
    );

    ckbtc.env.advance_time(MAX_TIME_IN_QUEUE);

    let mempool = ckbtc.mempool();
    assert_eq!(
        mempool.len(),
        0,
        "no transaction should appear when being reimbursed"
    );

    let reimbursement_block_index = block_index + 1;
    let reimbursement_amount = withdrawal_amount - BitcoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES;

    assert_matches!(
        ckbtc.retrieve_btc_status_v2(block_index),
        RetrieveBtcStatusV2::Reimbursed(reimbursement) if
        reimbursement.account == user_account &&
        reimbursement.amount == reimbursement_amount &&
        reimbursement.mint_block_index == reimbursement_block_index
    );
```
