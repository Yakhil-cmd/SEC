Audit Report

## Title
TOCTOU Blocklist Bypass: Pending Withdrawal Requests Not Re-Checked After Canister Upgrade — (`rs/ethereum/cketh/minter/src/withdraw.rs`)

## Summary

The ckETH minter enforces the blocklist exclusively at withdrawal submission time. Because the blocklist is a compile-time constant that only changes via canister upgrade, and because pending `AcceptedEthWithdrawalRequest` events are fully restored through `replay_events()` on upgrade with no re-validation, a withdrawal request accepted before an upgrade will be processed and sent to Ethereum after the upgrade — even if the destination address is now on the expanded blocklist.

## Finding Description

**Blocklist is compile-time only:**
`ETH_ADDRESS_BLOCKLIST` is a `const &[Address]` baked into the binary at `rs/ethereum/cketh/minter/src/blocklist.rs` L17. `is_blocked` performs a binary search over this static array at L107–109. The only way to change the blocklist is to deploy a new canister binary.

**Blocklist check only at ingress:**
`validate_address_as_destination` is called once at the start of `withdraw_eth` (main.rs L280–287) and `withdraw_erc20` (main.rs L407–414). After the check passes, the burn executes and `EventType::AcceptedEthWithdrawalRequest` is recorded (main.rs L330–335). No further blocklist check occurs.

**No blocklist check in the async processing pipeline:**
`create_transactions_batch` (withdraw.rs L249–293), `sign_transactions_batch` (withdraw.rs L303–339), and `send_transactions_batch` (withdraw.rs L340–384) all process pending requests with zero calls to `is_blocked`. These are invoked from `process_retrieve_eth_requests` on a timer (withdraw.rs L150–190).

**Pending withdrawal requests survive upgrades without re-validation:**
`post_upgrade` calls `replay_events()` (lifecycle/upgrade.rs L38–40), which calls `replay_events_internal` (audit.rs L189–204). For each `AcceptedEthWithdrawalRequest` event, `apply_state_transition` calls `record_withdrawal_request` directly (audit.rs L72–76) — with no call to `is_blocked`. The request is silently restored to the pending queue.

**Exploit flow:**
1. Attacker submits `withdraw_eth` to address X (not yet blocked). `AcceptedEthWithdrawalRequest` is recorded.
2. An NNS proposal to add X to the blocklist passes (NNS voting periods are days-long and fully public). The minter is upgraded with a new binary containing X in `ETH_ADDRESS_BLOCKLIST`.
3. `post_upgrade` → `replay_events()` → `apply_state_transition(AcceptedEthWithdrawalRequest)` → `record_withdrawal_request` — no blocklist check, X is back in the pending queue.
4. Timer fires → `process_retrieve_eth_requests` → `create_transactions_batch` → `sign_transactions_batch` → `send_transactions_batch` — ETH is sent to X on Ethereum.

## Impact Explanation

The stated invariant — *"ETH is not accepted from nor sent to addresses on this list"* (blocklist.rs L15) — is violated. ETH or ERC20 tokens are irreversibly sent on-chain to a newly-sanctioned address. This constitutes a direct, concrete sanctions-compliance failure for a production chain-fusion canister holding real ETH value. This matches the allowed impact: **High ($2,000–$10,000) — Significant Chain Fusion / ck-token security impact with concrete user or protocol harm.**

## Likelihood Explanation

The attack requires no privileged access. Any unprivileged user can submit a withdrawal. NNS proposals are public and the voting window (typically days) gives ample time to front-run the upgrade. The attacker only needs to observe a pending blocklist-expansion proposal and submit a withdrawal to the target address before the upgrade executes. The attack is repeatable for every new blocklist addition.

## Recommendation

Add a blocklist re-check inside `create_transactions_batch` before creating a transaction. If the destination is now blocked, cancel the withdrawal request and reimburse the user's ckETH (mirroring the existing `InsufficientTransactionFee` reimbursement path at withdraw.rs L281–291). The same check should be applied in `replay_events_internal` or at the point where pending requests are dequeued for processing, to prevent the request from ever re-entering the active queue post-upgrade.

## Proof of Concept

A state-machine test can confirm this:

1. Install the minter with an `ETH_ADDRESS_BLOCKLIST` that does **not** contain address `0xAAAA...`.
2. Call `withdraw_eth` with destination `0xAAAA...`; assert `AcceptedEthWithdrawalRequest` is recorded in the event log.
3. Upgrade the minter with a new WASM whose `ETH_ADDRESS_BLOCKLIST` includes `0xAAAA...`.
4. Verify `post_upgrade` → `replay_events_internal` restores the pending request without error (audit.rs L72–76 — no `is_blocked` call).
5. Advance the canister clock past `PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL`.
6. Assert that `eth_sendRawTransaction` is called with `to = 0xAAAA...` — confirming the transaction was sent to the now-blocked address. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L15-17)
```rust
/// ETH is not accepted from nor sent to addresses on this list.
/// NOTE: Keep it sorted!
const ETH_ADDRESS_BLOCKLIST: &[Address] = &[
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L107-109)
```rust
pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L72-76)
```rust
        EventType::AcceptedEthWithdrawalRequest(request) => {
            state
                .eth_transactions
                .record_withdrawal_request(request.clone());
        }
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L189-204)
```rust
fn replay_events_internal<T: IntoIterator<Item = Event>>(events: T) -> State {
    let mut events_iter = events.into_iter();
    let mut state = match events_iter
        .next()
        .expect("the event log should not be empty")
    {
        Event {
            payload: EventType::Init(init_arg),
            ..
        } => State::try_from(init_arg).expect("state initialization should succeed"),
        other => panic!("the first event must be an Init event, got: {other:?}"),
    };
    for event in events_iter {
        apply_state_transition(&mut state, &event.payload);
    }
    state
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
