### Title
Pre-`await` `panic!()` in ckETH Minter's `mint()` Bypasses Quarantine Guard and Permanently Blocks Cross-Chain Deposit Processing — (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter's `mint()` function contains a `panic!()` at line 63–66 that fires **before any `await` point** when an ERC-20 deposit event in `events_to_mint` has an unsupported contract address. The `scopeguard`-based quarantine mechanism at lines 43–52 is designed to protect against panics that occur **after** an `await` (i.e., in a callback), but it provides no protection for panics that occur before the first `await`. In IC Wasm, a pre-`await` panic causes the entire message execution to be rolled back — including the `TimerGuard` lock insertion into `active_tasks` — without running any `Drop` implementations. The event therefore remains in `events_to_mint`, the task lock is released, and the timer retries `mint()` on the next tick, hitting the same panic again. This creates a permanent, self-reinforcing DoS of all ckETH and ckERC20 deposit minting until the canister is upgraded.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function processes all pending deposit events:

```rust
async fn mint() {
    let _guard = match TimerGuard::new(TaskType::Mint) {   // line 32 — stored in replicated state
        Ok(guard) => guard,
        Err(_) => return,
    };
    let (eth_ledger_canister_id, events) = read_state(|s| ...);

    for event in events {
        // Guard designed to quarantine if panic occurs AFTER an await
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| process_event(s, EventType::QuarantinedDeposit { ... }));
        });                                                  // line 43–52

        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens.get_entry_alt(&event.erc20_contract_address)...
                }) {
                    result
                } else {
                    panic!(                                   // line 63–66 — BEFORE any await
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address..."
                    )
                }
            }
        };
        // First await point is here ↓
        let block_index = match client.transfer(...).await { ... };
``` [1](#0-0) 

The `TimerGuard` at line 32 stores its lock in `s.active_tasks`, which is part of the canister's replicated state (via `mutate_state`): [2](#0-1) 

The `active_tasks` field is declared as part of `State`: [3](#0-2) 

**IC-specific execution model**: In IC Wasm canisters compiled with `panic = "abort"` (the standard for Wasm targets), a `panic!()` invokes `ic0.trap`, which immediately aborts execution. There is **no stack unwinding**, so `Drop` implementations — including `scopeguard` — do **not** run. Furthermore, because the panic fires before the first `await` point, the IC runtime rolls back **all** state changes from the current message execution, including the `TimerGuard` insertion into `active_tasks`. The net result:

1. The `prevent_double_minting_guard` cleanup does **not** run → the event is **not** quarantined.
2. The `TimerGuard` lock is rolled back → `TaskType::Mint` is **not** permanently held.
3. The event remains in `events_to_mint` unchanged.
4. On the next timer tick, `mint()` is called again, hits the same panic, and the cycle repeats.

The comment in the code itself acknowledges the quarantine guard is for panics "in the callback" (i.e., after `await`): [4](#0-3) 

The `EventType::QuarantinedDeposit` variant documents this exact scenario — a panic during deposit processing that requires manual intervention: [5](#0-4) 

The `State::record_event_to_mint` function also contains an `assert!` that would panic if an unsupported ERC-20 address is encountered during state replay: [6](#0-5) 

---

### Impact Explanation

If the panic at line 63–66 is triggered, **all** pending ckETH and ckERC20 deposit minting is permanently blocked. Every timer tick calls `mint()`, which panics, rolls back, and retries — an infinite loop. No deposits are processed until the minter canister is upgraded via NNS governance proposal. This is directly analogous to the confirmed real-world ckBTC minter incident documented in the repository, where a deterministic panic in transaction resubmission blocked all ckBTC withdrawals: [7](#0-6) 

The impact is:
- **Availability**: All Ethereum → IC cross-chain deposits (ETH and all ckERC20 tokens) are blocked.
- **Funds at risk**: User funds deposited on Ethereum are not minted as ckETH/ckERC20 until the canister is upgraded.
- **Recovery**: Requires an NNS governance proposal to upgrade the minter, which takes days.

---

### Likelihood Explanation

**Low but non-zero.** The panic is labeled a "BUG" condition that "should have already been filtered out by `process_event`". Under normal operation, `record_event_to_mint` asserts the ERC-20 address is supported before adding it to `events_to_mint`, making the panic unreachable. However, the condition can arise through:

1. **Upgrade-induced state inconsistency**: If a future minter upgrade modifies `ckerc20_tokens` (e.g., removes or remaps a token) while `events_to_mint` still contains events for the old address, the invariant breaks.
2. **Stable memory deserialization bug**: A bug in CBOR deserialization of the event log during `replay_events_internal` could produce an inconsistent state where `events_to_mint` contains an address not in `ckerc20_tokens`.
3. **Precedent**: The ckBTC minter suffered an identical class of bug (deterministic panic blocking cross-chain operations) in production as recently as June 2025, demonstrating that such invariant violations do occur in practice. [8](#0-7) 

---

### Recommendation

Replace the `panic!()` at line 63–66 with graceful error handling that skips the problematic event (logging an error) and continues processing remaining events. Optionally, quarantine the deposit explicitly using `mutate_state` before continuing:

```rust
} else {
    log!(INFO, "BUG: unsupported ERC20 contract address for event {event:?}. Skipping.");
    // Quarantine to prevent infinite retry
    mutate_state(|s| process_event(s, EventType::QuarantinedDeposit {
        event_source: event.source(),
    }));
    ScopeGuard::into_inner(prevent_double_minting_guard);
    error_count += 1;
    continue;
}
```

This mirrors the pattern already used for ledger call failures at lines 85–101. [9](#0-8) 

---

### Proof of Concept

**Trigger path** (state inconsistency scenario):

1. NNS governance adds ckUSDC support → `AddedCkErc20Token` event recorded, `ckerc20_tokens` updated.
2. A user deposits USDC on Ethereum → minter scrapes the log → `AcceptedErc20Deposit` event recorded → event added to `events_to_mint` (assertion passes because USDC is supported at this point).
3. A minter upgrade is deployed that, due to a bug in upgrade argument handling or CBOR deserialization, fails to replay the `AddedCkErc20Token` event → `ckerc20_tokens` is empty but `events_to_mint` still contains the USDC deposit event.
4. The `mint()` timer fires → `read_state(|s| s.ckerc20_tokens.get_entry_alt(...))` returns `None` → `panic!()` at line 63–66.
5. IC runtime rolls back all state (including `TimerGuard` release) → event remains in `events_to_mint`.
6. Next timer tick → same panic → infinite loop.
7. All ckETH and ckERC20 deposits are blocked until a new NNS upgrade proposal is passed. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L32-68)
```rust
    let _guard = match TimerGuard::new(TaskType::Mint) {
        Ok(guard) => guard,
        Err(_) => return,
    };

    let (eth_ledger_canister_id, events) = read_state(|s| (s.cketh_ledger_id, s.events_to_mint()));
    let mut error_count = 0;

    for event in events {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this event will not be processed again.
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
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
            }
        };
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L85-101)
```rust
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L93-110)
```rust
impl TimerGuard {
    pub fn new(task: TaskType) -> Result<Self, TimerGuardError> {
        mutate_state(|s| {
            if !s.active_tasks.insert(task) {
                return Err(TimerGuardError::AlreadyProcessing);
            }
            Ok(Self { task })
        })
    }
}

impl Drop for TimerGuard {
    fn drop(&mut self) {
        mutate_state(|s| {
            s.active_tasks.remove(&self.task);
        });
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L80-83)
```rust

    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: HashSet<TaskType>,

```

**File:** rs/ethereum/cketh/minter/src/state.rs (L199-205)
```rust
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
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

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L31-33)
```markdown
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L185-204)
```rust
pub fn replay_events() -> State {
    with_event_iter(|iter| replay_events_internal(iter))
}

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
