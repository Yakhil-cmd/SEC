Audit Report

## Title
Quarantined ETH/ERC20 Deposits Have No Refund or Retry Mechanism - (File: rs/ethereum/cketh/minter/src/deposit.rs)

## Summary
The ckETH/ckERC20 minter permanently quarantines deposits when a panic occurs during the minting callback, recording `EventType::QuarantinedDeposit` and removing the event from `events_to_mint` with no automatic recovery path. User ETH or ERC20 tokens already transferred to the minter's Ethereum address are irrecoverably locked until a governance-approved canister upgrade manually processes the quarantined event.

## Finding Description
In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function arms a `scopeguard` before each ledger transfer call: [1](#0-0) 

If a panic occurs in the callback after the `.await` on `client.transfer(...)`, the guard fires and records `EventType::QuarantinedDeposit`. The `record_quarantined_deposit` method in `rs/ethereum/cketh/minter/src/state.rs` then removes the event from `events_to_mint` and inserts it into `invalid_events` under `InvalidEventReason::QuarantinedDeposit`: [2](#0-1) 

The event type itself is documented as requiring manual intervention and will never be retried automatically: [3](#0-2) 

A concrete panic path exists within `mint()` itself: if an ERC20 deposit event passes `events_to_mint()` but the ERC20 contract address is no longer in `ckerc20_tokens` at callback time (e.g., due to a concurrent state change), the code explicitly panics: [4](#0-3) 

The `process_reimbursement()` function in `rs/ethereum/cketh/minter/src/withdraw.rs` handles only withdrawal reimbursements via `reimbursement_requests_iter()` and has no path for quarantined deposits: [5](#0-4) 

No `schedule_deposit_reimbursement` endpoint, no deposit reimbursement queue, and no `QuarantinedDeposit → ReimbursedDeposit` state transition exists anywhere in the minter codebase. By contrast, the withdrawal path uses `QuarantinedReimbursement` with a symmetric recovery pattern, and the ckBTC minter implements a full `pending_withdrawal_reimbursements` queue. No equivalent exists for ckETH/ckERC20 deposits.

## Impact Explanation
**High.** This matches the allowed impact class: "Significant Chain Fusion, ck-token, ledger, Rosetta, boundary/API, XRC, Internet Identity, NNS, SNS, or infrastructure security impact with concrete user or protocol harm." Any user who deposits ETH or a supported ERC20 token via the helper smart contract irrevocably transfers custody of those assets to the minter's Ethereum address at the Ethereum layer. If the IC minting callback panics for any reason after the ledger transfer call is issued, the deposit is permanently quarantined. The user receives no ckETH/ckERC20 and has no on-chain mechanism to recover their funds. Recovery requires a governance-approved NNS proposal to upgrade the minter canister and manually process the quarantined event — a process that may take days and is not guaranteed.

## Likelihood Explanation
**Low-Medium.** The panic is not directly user-controllable, but is reachable through realistic operational scenarios: (1) a ledger canister upgrade that changes the Candid response encoding, causing a deserialization trap in the callback; (2) a replica-level out-of-cycles or memory trap during the inter-canister call; (3) the explicit `panic!` at line 63–66 of `deposit.rs` if an ERC20 contract address is removed from `ckerc20_tokens` between event acceptance and minting. Historical precedent confirms that external-dependency failures affecting the ckETH minter do occur in production (the 2024-03-18 Cloudflare RPC incident). The condition affects all users whose deposits are in-flight at the time of the failure.

## Recommendation
1. Implement a `schedule_deposit_reimbursement`-style queue for quarantined deposits. When `EventType::QuarantinedDeposit` is recorded, store the depositor's IC principal and deposit amount so a future reimbursement mint can be issued.
2. Add a `QuarantinedDeposit → ReimbursedDeposit` state transition with appropriate double-minting guards, mirroring the `QuarantinedReimbursement` / `ReimbursedEthWithdrawal` pattern already used for withdrawals in `rs/ethereum/cketh/minter/src/withdraw.rs`.
3. Expose a canister endpoint (callable by the minter itself on a timer, or by NNS governance) to trigger reimbursement for quarantined deposits, analogous to how ckBTC handles `pending_withdrawal_reimbursements`.

## Proof of Concept
1. User calls `depositEth(amount, principal, subaccount)` on the Ethereum helper contract. ETH is transferred to the minter's Ethereum address; a `ReceivedEth` log event is emitted.
2. The ckETH minter scrapes the log and records `EventType::AcceptedDeposit`. The event enters `events_to_mint`.
3. On the next timer tick, `mint()` is called. The `prevent_double_minting_guard` is armed for the deposit event.
4. `client.transfer(...)` is called to the ckETH ledger. The call crosses an async message boundary (`.await`).
5. A panic occurs in the callback — e.g., the ledger is upgraded between step 4 and the response, returning a Candid encoding that fails deserialization, or the minter runs out of cycles during the callback.
6. The `scopeguard` fires, recording `EventType::QuarantinedDeposit { event_source }`. `record_quarantined_deposit` removes the event from `events_to_mint` and inserts it into `invalid_events`. [2](#0-1) 

7. The deposit source is permanently excluded from future `events_to_mint()` calls. The user's ETH remains in the minter's Ethereum address. No ckETH is minted. No refund is issued.
8. The `QuarantinedDeposit` event is observable via `get_events` in the minter's append-only event log, confirming the stuck state.

A deterministic integration test can reproduce this by: (a) accepting a deposit event into state, (b) simulating a panic in the minting callback using `std::panic::catch_unwind` or a PocketIC test that traps the minter canister mid-callback, and (c) asserting that `events_to_mint` is empty, `invalid_events` contains the source under `QuarantinedDeposit`, and no reimbursement queue entry exists.

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L43-52)
```rust
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::QuarantinedDeposit {
                        event_source: event.source(),
                    },
                )
            });
        });
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L63-66)
```rust
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L244-253)
```rust
    fn record_quarantined_deposit(&mut self, source: EventSource) -> bool {
        self.events_to_mint.remove(&source);
        match self.invalid_events.entry(source) {
            btree_map::Entry::Occupied(_) => false,
            btree_map::Entry::Vacant(entry) => {
                entry.insert(InvalidEventReason::QuarantinedDeposit);
                true
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L141-149)
```rust
    /// The minter unexpectedly panic while processing a deposit.
    /// The deposit is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(21)]
    QuarantinedDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
    },
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L55-63)
```rust
    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
    if reimbursements.is_empty() {
        return;
    }
```
