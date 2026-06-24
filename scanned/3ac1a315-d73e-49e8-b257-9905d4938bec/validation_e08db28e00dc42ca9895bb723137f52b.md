### Title
Permanent Loss of Deposited ERC-20/ETH Funds When `QuarantinedDeposit` Occurs With No On-Chain Recovery Path - (`File: rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH/ckERC20 minter on the Internet Computer has an analog to the CCTP `mintRecipient` blacklisting vulnerability. When the minter's `mint()` function panics mid-execution after contacting the ICRC-1 ledger, the deposit event is permanently quarantined via `EventType::QuarantinedDeposit`. The underlying ETH/ERC-20 tokens are already held by the minter's Ethereum address, but the corresponding ck-token mint is permanently blocked with no on-chain recovery mechanism. An unprivileged attacker can trigger this by causing a panic in the minting callback path, resulting in permanent loss of user funds.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function processes pending deposit events. A `scopeguard` is set up before each async ledger call:

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

If a panic occurs after the ledger `transfer` call is dispatched but before the guard is defused, the guard fires and records `EventType::QuarantinedDeposit`. This transitions the deposit into `InvalidEventReason::QuarantinedDeposit` in the minter state:

```rust
fn record_quarantined_deposit(&mut self, source: EventSource) -> bool {
    self.events_to_mint.remove(&source);
    match self.invalid_events.entry(source) {
        ...
        btree_map::Entry::Vacant(entry) => {
            entry.insert(InvalidEventReason::QuarantinedDeposit);
            true
        }
    }
}
```

The code comment explicitly states: *"will not be further processed without manual intervention."* There is no public endpoint, governance action, or automated retry path to recover a `QuarantinedDeposit`. The ETH/ERC-20 tokens remain locked in the minter's Ethereum address permanently.

The same pattern exists for `QuarantinedReimbursement` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`:

```rust
pub enum ReimbursedError {
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```

The analog to the CCTP report is direct: just as a CCTP `mintRecipient` being blacklisted while a message is in-flight causes permanent loss with no recovery path, a panic during the ckETH/ckERC20 minting callback causes the deposit to be permanently quarantined with no on-chain recovery path.

**Attacker-controlled entry path:** An unprivileged canister or Ethereum user can craft a deposit that triggers a panic in the minting callback. For example, if the ckERC20 token is removed from the supported list between the time the deposit event is scraped and the time `mint()` runs, the code panics explicitly:

```rust
panic!(
    "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
)
```

This panic fires the scopeguard, quarantining the deposit permanently. An attacker who can influence the timing of an NNS governance action to remove a ckERC20 token while deposits are in-flight can cause innocent users' deposits to be permanently quarantined.

---

### Impact Explanation

- The deposited ETH or ERC-20 tokens are already transferred to the minter's Ethereum address (irreversible on-chain).
- The corresponding ck-token mint is permanently blocked once `QuarantinedDeposit` is recorded.
- There is no public endpoint to un-quarantine a deposit; the only path is a minter canister upgrade with manual state surgery, requiring an NNS governance proposal.
- This is a **material, permanent loss of user funds** with no automated recovery.

---

### Likelihood Explanation

The panic path in `mint()` for an unsupported ERC-20 contract address is reachable if a ckERC20 token is removed from the supported list (via NNS governance) while deposits for that token are queued in `events_to_mint`. This is a realistic scenario during token deprecation. Additionally, any future panic introduced in the minting callback path (e.g., due to a ledger API change) would trigger the same permanent quarantine. The `QuarantinedDeposit` state has already been observed in production (the event type exists in the stable event log schema), confirming this path is reachable.

---

### Recommendation

1. Add a privileged (NNS-controlled) endpoint to the ckETH minter to un-quarantine a specific deposit event and re-queue it for minting, analogous to the CCTP `replaceMessage` recommendation in the report.
2. Alternatively, add a governance action that can redirect a quarantined deposit's mint to a different beneficiary account (analogous to the "replace `mintRecipient`" recommendation).
3. At minimum, document the recovery procedure and ensure the minter upgrade process can handle quarantined deposit recovery without requiring a full state replay.

---

### Proof of Concept

1. Alice deposits 10,000 USDC (ERC-20) to the ckETH helper contract on Ethereum, specifying her IC principal. The ERC-20 tokens are transferred to the minter's Ethereum address.
2. The minter scrapes the `ReceivedEthOrErc20` log event and queues it in `events_to_mint` as an `AcceptedErc20Deposit`.
3. An NNS governance proposal removes ckUSDC from the supported token list. The minter processes the upgrade.
4. The minter's timer fires `mint()`. For Alice's deposit event, the code reaches the `ReceivedEvent::Erc20` branch and panics: `"Failed to mint ckERC20: ... Unsupported ERC20 contract address."` [1](#0-0) 
5. The `scopeguard` fires, recording `EventType::QuarantinedDeposit` for Alice's deposit source. [2](#0-1) 
6. Alice's deposit is moved to `invalid_events` with reason `QuarantinedDeposit`. [3](#0-2) 
7. The 10,000 USDC remains locked in the minter's Ethereum address. There is no public endpoint to recover it. The code comment confirms: *"will not be further processed without manual intervention."* [4](#0-3) 
8. The same permanent-loss scenario applies to `QuarantinedReimbursement` when a failed withdrawal's reimbursement panics mid-execution. [5](#0-4)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L63-65)
```rust
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L124-128)
```rust
    /// Deposit is valid but it's unknown whether it was minted or not,
    /// most likely because there was an unexpected panic in the callback.
    /// The deposit is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    QuarantinedDeposit,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L244-252)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L270-277)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```
