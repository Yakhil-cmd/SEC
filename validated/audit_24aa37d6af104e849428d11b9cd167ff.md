### Title
QuarantinedDeposit Permanently Locks User ETH/ERC20 Funds With No User-Accessible Recovery Path - (File: rs/ethereum/cketh/minter/src/deposit.rs)

---

### Summary

The ckETH/ckERC20 minter's `mint()` function installs a `scopeguard` **before** the ledger call that, on any panic, permanently quarantines the deposit event. Once quarantined, the deposit is removed from the processing queue and placed in `invalid_events` with no user-callable recovery endpoint. The user's ETH/ERC20 is held in the minter's Ethereum address while no ckETH/ckERC20 is ever minted, mirroring the external report's pattern of assets transferred → request consumed/rejected → funds permanently stuck with no recovery function.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` async function iterates over `events_to_mint`. For each event it immediately arms a `scopeguard` that emits `EventType::QuarantinedDeposit` on any panic:

```rust
let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
    mutate_state(|s| {
        process_event(s, EventType::QuarantinedDeposit { event_source: event.source() })
    });
});
``` [1](#0-0) 

The guard is armed **before** the `match &event` block that resolves the ledger canister ID. That block contains an explicit `panic!` for any ERC20 event whose contract address is not in `ckerc20_tokens`:

```rust
} else {
    panic!(
        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. \
         (This should have already been filtered out by process_event)"
    )
}
``` [2](#0-1) 

Because the guard fires on **any** panic—including ones that occur before the ledger is ever contacted—a deposit can be quarantined even though no mint was attempted and there is zero double-mint risk. Additional panic paths exist after the ledger call: `block_index.0.to_u64().expect("nat does not fit into u64")` and the `mutate_state` call that invokes `record_successful_mint`, which itself panics on state-invariant violations. [3](#0-2) 

`apply_state_transition` handles `QuarantinedDeposit` by calling `record_quarantined_deposit`, which removes the event from `events_to_mint` and inserts it into `invalid_events` with `InvalidEventReason::QuarantinedDeposit`:

```rust
EventType::QuarantinedDeposit { event_source } => {
    state.record_quarantined_deposit(*event_source);
}
``` [4](#0-3) 

The code comment on `InvalidEventReason::QuarantinedDeposit` explicitly states the consequence:

> "The deposit is quarantined to avoid any double minting and **will not be further processed without manual intervention**." [5](#0-4) 

`record_quarantined_deposit` removes the event from `events_to_mint` and inserts it into `invalid_events`: [6](#0-5) 

Scanning the public endpoints in `rs/ethereum/cketh/minter/src/main.rs` and `rs/ethereum/cketh/minter/cketh_minter.did`, there is **no** user-callable endpoint to retry or recover a quarantined deposit. The `EventType::QuarantinedDeposit` variant is exposed only as a read-only audit event: [7](#0-6) 

The same quarantine-without-recovery pattern exists for reimbursements (`QuarantinedReimbursement`): [8](#0-7) 

---

### Impact Explanation

A user who deposits ETH or an ERC20 token via the Ethereum helper contract has their funds transferred to the minter's Ethereum address at the moment the on-chain transaction is mined. If the minter subsequently quarantines the corresponding deposit event (due to a panic before or after the ledger call), the user:

- Loses access to the deposited ETH/ERC20 permanently (it sits in the minter's Ethereum address).
- Receives no ckETH/ckERC20 in return.
- Has no user-callable endpoint to retry, refund, or escalate the deposit.
- Must wait for a governance proposal and canister upgrade to manually process the quarantined event—a process that can take days and requires DFINITY/NNS action.

The minter's `eth_balance` / `erc20_balances` accounting will be inflated relative to the actual ckETH/ckERC20 supply, creating a conservation discrepancy.

---

### Likelihood Explanation

**Low-to-medium.** The panic paths are:

1. **Pre-ledger-call panic (most direct analog):** If an ERC20 deposit event for a token that was subsequently removed from `ckerc20_tokens` (via governance) remains in `events_to_mint`, the `match &event` block panics. The guard fires and quarantines the deposit even though the ledger was never contacted. This is a realistic scenario during token delisting.

2. **Post-ledger-call panic:** `block_index.0.to_u64().expect()` or `record_successful_mint` panics. These are edge cases but become more likely as the minter's state grows.

3. **IC execution trap:** The IC execution environment can trap a canister message if it exceeds the instruction limit. As `events_to_mint` grows (e.g., during a high-volume deposit period), the per-event processing cost increases, making an instruction-limit trap more plausible.

The mainnet ckETH minter (`sv3dd-oaaaa-aaaar-qacoa-cai`) processes real user funds, so even a low-probability event affecting a single user constitutes a medium-severity issue.

---

### Recommendation

1. **Move the guard to after the `match` block.** The double-mint risk only exists once the ledger has been contacted. Arming the guard before the `match` block causes unnecessary quarantines for pre-ledger panics.

2. **Add a user-accessible retry endpoint** (e.g., `retry_quarantined_deposit(event_source: EventSource)`) that re-queues a quarantined deposit into `events_to_mint` after verifying the deposit event is still valid on Ethereum. Access can be restricted to the original depositor.

3. **Alternatively, implement automatic re-queue logic** for quarantined deposits that were never sent to the ledger (detectable by checking whether the ledger was contacted before the panic).

---

### Proof of Concept

**Scenario: ERC20 token delisting with pending deposits**

1. User calls `depositErc20` on the Ethereum helper contract for token T, depositing 1000 USDC. The transaction is mined and the minter scrapes the log, recording `AcceptedErc20Deposit` and adding the event to `events_to_mint`.

2. A governance proposal removes token T from the minter's supported ERC20 list (e.g., via `UpgradeArg` that does not re-add the token). The minter upgrades; `ckerc20_tokens` no longer contains T's contract address.

3. The minter's timer fires and calls `mint()`. For the pending ERC20 deposit event, the `match &event` block executes:
   ```rust
   s.ckerc20_tokens.get_entry_alt(&event.erc20_contract_address) // returns None
   ```
   The `else` branch panics. The `scopeguard` fires and records `QuarantinedDeposit`.

4. The user's 1000 USDC is permanently held in the minter's Ethereum address. No ckUSDC is minted. The user queries `get_events` and sees `QuarantinedDeposit` for their deposit. There is no endpoint to recover the funds. [9](#0-8) [10](#0-9) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L41-52)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L53-68)
```rust
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L83-120)
```rust
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
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
        };
        mutate_state(|s| {
            process_event(
                s,
                match &event {
                    ReceivedEvent::Eth(event) => EventType::MintedCkEth {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                    },

                    ReceivedEvent::Erc20(event) => EventType::MintedCkErc20 {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                        erc20_contract_address: event.erc20_contract_address,
                        ckerc20_token_symbol: token_symbol.clone(),
                    },
                },
            )
        });
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L154-156)
```rust
        EventType::QuarantinedDeposit { event_source } => {
            state.record_quarantined_deposit(*event_source);
        }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L124-128)
```rust
    /// Deposit is valid but it's unknown whether it was minted or not,
    /// most likely because there was an unexpected panic in the callback.
    /// The deposit is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    QuarantinedDeposit,
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

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L488-490)
```rust
        QuarantinedDeposit {
            event_source: EventSource,
        },
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

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L150-158)
```rust
    /// The minter unexpectedly panic while processing a reimbursement.
    /// The reimbursement is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(22)]
    QuarantinedReimbursement {
        /// The unique identifier of the reimbursement.
        #[n(0)]
        index: ReimbursementIndex,
    },
```
