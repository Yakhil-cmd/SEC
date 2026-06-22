### Title
No Mechanism to Remove a Supported ckERC20 Token from the ckETH Minter - (`File: rs/ethereum/cketh/minter/src/state/event.rs`)

---

### Summary

The ckETH minter canister exposes `add_ckerc20_token` to register new ERC-20 tokens as supported, but provides no corresponding removal endpoint. Once a token is added, neither the NNS nor the ledger suite orchestrator can remove it. This is the direct IC analog of the ZetaChain `unwhitelist` issue: the "un-whitelist" path is entirely absent rather than merely unreachable.

---

### Finding Description

The `EventType` enum in `rs/ethereum/cketh/minter/src/state/event.rs` defines `AddedCkErc20Token` [1](#0-0)  but contains no `RemovedCkErc20Token` variant. The state machine in `apply_state_transition` handles `AddedCkErc20Token` by calling `record_add_ckerc20_token` [2](#0-1)  but has no corresponding removal branch. The minter's Candid interface exposes `add_ckerc20_token` [3](#0-2)  but no `remove_ckerc20_token`. The `record_add_ckerc20_token` method on `State` inserts into `ckerc20_tokens` [4](#0-3)  with no deletion counterpart.

Because the minter state is fully reconstructed by replaying the event log via `replay_events_internal` [5](#0-4) , a token added via `AddedCkErc20Token` will survive every future upgrade: the event is permanent and there is no compensating event to replay.

The ledger suite orchestrator's `notify_erc20_added` task calls `add_ckerc20_token` on the minter [6](#0-5)  but there is no symmetric `notify_erc20_removed` task or endpoint.

---

### Impact Explanation

If a supported ERC-20 token's on-chain contract is compromised (e.g., an infinite-mint exploit), the minter will continue to scrape its logs and mint ckERC20 tokens for every deposit event it observes. Because `record_event_to_mint` asserts that the ERC-20 address is in `ckerc20_tokens` [7](#0-6) , any deposit from the compromised contract will be accepted and minted. The NNS has no governance path to halt this: it cannot submit a proposal to remove the token because neither the orchestrator nor the minter exposes such an endpoint. The result is unbounded minting of ckERC20 tokens backed by worthless or attacker-controlled ERC-20 tokens, breaking the 1:1 backing invariant of the chain-fusion bridge.

---

### Likelihood Explanation

ERC-20 contracts on Ethereum have a documented history of post-deployment exploits (reentrancy, infinite-mint, ownership takeover). The ckERC20 system already supports multiple high-value tokens (USDC, USDT, WBTC, etc.). A single compromised token contract, combined with the absence of a removal path, is sufficient to trigger unbounded ckERC20 minting. The likelihood is low-to-medium: it requires an external ERC-20 compromise, but the IC-side design gap means there is no emergency response available once such a compromise occurs.

---

### Recommendation

1. Add a `RemovedCkErc20Token` variant to `EventType` in `rs/ethereum/cketh/minter/src/state/event.rs`.
2. Implement `record_remove_ckerc20_token` on `State` and handle the new event in `apply_state_transition`.
3. Expose a `remove_ckerc20_token` update endpoint on the minter, restricted to the orchestrator ID (mirroring `add_ckerc20_token`).
4. Add a corresponding `notify_erc20_removed` task to the ledger suite orchestrator so that an NNS upgrade proposal can trigger removal end-to-end.

---

### Proof of Concept

1. NNS passes a proposal to add `ckCOMPROMISED` via the ledger suite orchestrator. The orchestrator calls `add_ckerc20_token` on the minter; `AddedCkErc20Token` is appended to the event log. [8](#0-7) 
2. The underlying ERC-20 contract is later exploited; an attacker mints 10^9 tokens to their address.
3. The attacker calls `depositErc20` on the helper contract, emitting `ReceivedEthOrErc20` events.
4. The minter's log-scraping timer picks up these events. Because the ERC-20 address is still in `ckerc20_tokens`, `record_event_to_mint` accepts them and the minter mints ckERC20 tokens to the attacker's IC principal. [9](#0-8) 
5. The NNS attempts to halt minting by removing the token. No such proposal type exists. The minter continues minting on every timer tick until the event log is exhausted or the minter is stopped entirely—a drastic action that also halts all other ckERC20 and ckETH operations.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L100-102)
```rust
    /// Add a new ckERC20 token.
    #[n(14)]
    AddedCkErc20Token(#[n(0)] CkErc20Token),
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L126-128)
```rust
        EventType::AddedCkErc20Token(ckerc20_token) => {
            state.record_add_ckerc20_token(ckerc20_token.clone());
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L744-746)
```text
    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L199-209)
```rust
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
        }

        self.events_to_mint.insert(event_source, event.clone());

        self.update_balance_upon_deposit(event)
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L415-423)
```rust
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1141-1144)
```rust
            runtime
                .call_canister(*minter_id, "add_ckerc20_token", args)
                .await
                .map_err(TaskError::InterCanisterCallError)
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-573)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
```
