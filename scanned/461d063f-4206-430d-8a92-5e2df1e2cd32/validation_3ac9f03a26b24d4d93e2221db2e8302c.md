### Title
Unbounded Event Log Replay on `post_upgrade` Enables Upgrade-Blocking DoS — (`File: rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs`)

---

### Summary

The ckETH minter canister (and analogously the ckBTC minter) reconstructs its entire in-memory state by replaying every event ever recorded in its stable-memory event log on every `post_upgrade`. Because the event log grows unboundedly with normal user activity and autonomous timer-driven scraping, the instruction cost of `post_upgrade` grows linearly with the log size. Once the log is large enough, `post_upgrade` will exceed `MAX_INSTRUCTIONS_PER_INSTALL_CODE` (300 billion instructions on application subnets), causing every future upgrade attempt to trap and permanently preventing the canister from being patched or upgraded.

---

### Finding Description

**ckETH minter — `post_upgrade` replays all events unconditionally:** [1](#0-0) 

```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();
    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());   // ← replays from event 0
    });
    ...
}
```

`replay_events()` calls `replay_events_internal`, which iterates over every event in the stable-memory log with no offset or snapshot shortcut: [2](#0-1) 

The event log is stored in a `StableLog` that is append-only and never pruned: [3](#0-2) 

**ckBTC minter — identical pattern, with additional per-event invariant checking:** [4](#0-3) 

The ckBTC replay additionally runs `CheckInvariantsImpl` after each event, making per-event cost higher.

**Hard instruction cap for upgrades:** [5](#0-4) 

```
const MAX_INSTRUCTIONS_PER_INSTALL_CODE: NumInstructions = NumInstructions::new(300 * B);
```

DTS allows the upgrade to span multiple rounds, but the total across all rounds is still bounded by `max_instructions_per_install_code`.

**Event log grows automatically without any user action:**

The ckETH minter emits `SyncedToBlock`, `SyncedErc20ToBlock`, and `SyncedDepositWithSubaccountToBlock` events on every scraping timer tick (independent of user deposits or withdrawals): [6](#0-5) 

A production snapshot already contains **49,263 events**: [7](#0-6) 

---

### Impact Explanation

If the event log grows large enough that replaying it consumes more than `MAX_INSTRUCTIONS_PER_INSTALL_CODE` instructions, every call to `install_code` (upgrade) on the minter will fail with `CanisterInstructionLimitExceeded`. The canister becomes permanently frozen at its current Wasm version: no security patches, no bug fixes, no parameter changes can be applied. Because the ckETH and ckBTC minters are the sole custodians of bridged funds (they hold the signing keys and control minting/burning), a frozen minter is a critical availability failure for the entire chain-fusion bridge.

---

### Likelihood Explanation

The event log grows on two independent axes:

1. **Autonomous growth** — `SyncedToBlock` / `SyncedErc20ToBlock` events are emitted by the minter's own timer on every scraping interval, regardless of user activity. Over years of operation this alone produces millions of events.
2. **User-accelerated growth** — any non-anonymous principal can call `update_balance` (ckBTC) or `withdraw_eth` (ckETH) to inject additional events. Each successful call appends multiple events (accepted request, created tx, signed tx, finalized tx). The cost to the attacker is the on-chain transaction fee, not a protocol-level resource. [8](#0-7) [9](#0-8) 

The current count of ~49 K events is well below the limit today, but the log has no upper bound and no pruning path. The risk materialises gradually and irreversibly.

---

### Recommendation

**Short term:** Introduce a persisted "replay checkpoint": after each successful `post_upgrade`, serialize the fully-replayed state into a dedicated stable-memory region (e.g., a `StableCell`) and record the event-log offset at which the snapshot was taken. On the next upgrade, deserialize the snapshot and replay only events appended after the snapshot offset. The snapshot and offset must be written atomically.

**Long term:** Add an integration test that loads a large synthetic event log (e.g., 10 M events) and asserts that `post_upgrade` completes within the instruction budget. Add a canister metric that tracks the current event count and emits an alert when it approaches a configurable threshold.

---

### Proof of Concept

1. Observe that `post_upgrade` unconditionally calls `replay_events()` / `event_logger.replay(event_logger.events_iter())` with no offset:
   - ckETH: `rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs:39`
   - ckBTC: `rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs:94`

2. Observe that the stable-memory log is append-only with no pruning:
   - `rs/ethereum/cketh/minter/src/storage.rs:50-58` (`record_event` appends; no trim path exists)

3. Observe that `SyncedToBlock` events are emitted on every scraping timer tick, growing the log autonomously.

4. Observe the hard cap: `MAX_INSTRUCTIONS_PER_INSTALL_CODE = 300 * B` (`rs/config/src/subnet_config.rs:85`).

5. Extrapolate: at 49,263 events today, if per-event replay cost is C instructions, the log will exhaust the budget when event count reaches `300 × 10⁹ / C`. Because C grows with state complexity (BTreeMap insertions, invariant checks in ckBTC), the budget will eventually be exhausted as the protocol matures, and can be accelerated by any unprivileged caller making repeated deposits or withdrawals.

### Citations

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L35-54)
```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();

    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());
    });
    if let Some(args) = upgrade_args {
        mutate_state(|s| process_event(s, EventType::Upgrade(args)))
    }

    let end = ic_cdk::api::instruction_counter();

    let event_count = total_event_count();
    let instructions_consumed = end - start;

    log!(
        INFO,
        "[upgrade]: replaying {event_count} events consumed {instructions_consumed} instructions ({} instructions per event on average)",
        instructions_consumed / event_count
    );
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L61-71)
```rust
        EventType::SyncedToBlock { block_number } => {
            state.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::EthDepositWithoutSubaccount,
                *block_number,
            );
        }
        EventType::SyncedErc20ToBlock { block_number } => {
            state
                .log_scrapings
                .set_last_scraped_block_number(Erc20DepositWithoutSubaccount, *block_number);
        }
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

**File:** rs/ethereum/cketh/minter/src/storage.rs (L37-59)
```rust
    /// The log of the ckETH state modifications.
    static EVENTS: RefCell<EventLog> = MEMORY_MANAGER
        .with(|m|
              RefCell::new(
                  StableLog::init(
                      m.borrow().get(LOG_INDEX_MEMORY_ID),
                      m.borrow().get(LOG_DATA_MEMORY_ID)
                  ).expect("failed to initialize stable log")
              )
        );
}

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

**File:** rs/ethereum/cketh/minter/src/storage.rs (L95-100)
```rust
        let event_count = total_event_count();
        assert_eq!(event_count, 49_263, "expected events in stable memory");

        canbench_rs::bench_fn(|| {
            crate::lifecycle::upgrade::post_upgrade(None);
        })
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L91-97)
```rust
    let event_logger = runtime.event_logger();

    let state = event_logger
        .replay::<CheckInvariantsImpl>(event_logger.events_iter())
        .unwrap_or_else(|e| {
            ic_cdk::trap(format!("[upgrade]: failed to replay the event log: {e:?}"))
        });
```

**File:** rs/config/src/subnet_config.rs (L83-85)
```rust
// Limit per `install_code` message. It's bigger than the limit for a regular
// update call to allow for canisters with bigger state to be upgraded.
const MAX_INSTRUCTIONS_PER_INSTALL_CODE: NumInstructions = NumInstructions::new(300 * B);
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-153)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```
