### Title
Cross-Network Deposit Replay in ckETH/ckERC-20 Minter: `EventSource` Uniqueness Assumption Breaks Across Ethereum Networks - (File: `rs/ethereum/cketh/minter/src/eth_logs/mod.rs`)

---

### Summary

The ckETH minter's `EventSource` deduplication key — composed solely of `(transaction_hash, log_index)` — is documented as "globally unique" but this claim only holds within a single Ethereum network. The same `(transaction_hash, log_index)` pair can legitimately appear on both Ethereum Mainnet and Sepolia (or any future supported network), because Ethereum transaction hashes are not globally unique across chains. If the minter's `ethereum_network` configuration is ever changed (e.g., via upgrade), or if a future code path allows the same minter state to process events from multiple networks, a deposit event from one network can suppress or collide with a deposit event from another network, causing either a missed mint or a double-mint.

---

### Finding Description

The `EventSource` struct is the sole deduplication key used by the ckETH minter to prevent double-minting:

```rust
// rs/ethereum/cketh/minter/src/eth_logs/mod.rs, lines 124-132
/// A unique identifier of the event source: the source transaction hash and the log
/// entry index.
pub struct EventSource {
    pub transaction_hash: Hash,
    pub log_index: LogIndex,
}
```

The code comment at line 159–162 explicitly claims:

> "Return event source, which is globally unique regardless of whether it is for ETH or ERC-20. This is because the `transaction_hash` already unique determines the transaction, and `log_index` would match the place in which event appears for this transaction."

This claim is **false across Ethereum networks**. Ethereum transaction hashes are computed as `keccak256(RLP(signed_tx))`. The same signed transaction bytes (same sender nonce, same recipient, same value, same gas params, same signature) produce the **identical transaction hash** on both Mainnet and Sepolia. This is the exact analog of the cross-chain replay vulnerability in the external report: a hash that does not commit to the chain/network identifier.

The minter state stores seen events in three `BTreeMap<EventSource, ...>` fields:

```rust
// rs/ethereum/cketh/minter/src/state.rs, lines 64-66
pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
pub minted_events: BTreeMap<EventSource, MintedEvent>,
pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
```

The `record_event_to_mint` function asserts uniqueness of `EventSource` without any network binding:

```rust
// rs/ethereum/cketh/minter/src/state.rs, lines 191-209
fn record_event_to_mint(&mut self, event: &ReceivedEvent) {
    let event_source = event.source();
    assert!(
        !self.events_to_mint.contains_key(&event_source),
        "there must be no two different events with the same source"
    );
    assert!(!self.minted_events.contains_key(&event_source));
    assert!(!self.invalid_events.contains_key(&event_source));
    ...
    self.events_to_mint.insert(event_source, event.clone());
```

The `ethereum_network` field exists in `State` but is **never included** in the `EventSource` key. The `EthereumNetwork` enum encodes the chain ID:

```rust
// rs/ethereum/cketh/minter/src/lifecycle.rs, lines 35-40
pub fn chain_id(&self) -> u64 {
    match self {
        EthereumNetwork::Mainnet => 1,
        EthereumNetwork::Sepolia => 11155111,
    }
}
```

The concrete attack surface is the **minter upgrade path**: `UpgradeArg` does not include `ethereum_network`, meaning the network is set at `Init` time and cannot be changed. However, the `EventSource` deduplication maps persist across upgrades. If a minter is initialized on Sepolia, accumulates `minted_events` for certain `(tx_hash, log_index)` pairs, and is then re-initialized (via state replay or a new deployment) on Mainnet with the same stable memory, those Sepolia-minted event sources would incorrectly suppress Mainnet deposit events with the same `(tx_hash, log_index)`.

Additionally, the `ReceivedEvent::source()` comment explicitly states the assumption of global uniqueness without qualifying it to a single chain, which is a documented incorrect invariant that future developers may rely upon.

---

### Impact Explanation

**Chain-fusion mint suppression (missed mint):** If a Mainnet deposit event has the same `(transaction_hash, log_index)` as a previously processed Sepolia event (possible because Ethereum tx hashes are not chain-scoped), the minter will silently skip minting ckETH/ckERC-20 for the Mainnet depositor. The user loses funds with no error returned.

**Double-mint via state migration:** If a minter's stable memory (event log) is replayed on a different network (e.g., during disaster recovery or a misconfigured re-deployment), Sepolia events in `minted_events` would block Mainnet events with the same source from being minted, or vice versa — depending on replay order, could cause double-minting.

**Severity:** Medium. The immediate practical risk is low because the minter is initialized once per network and the `ethereum_network` cannot be changed via upgrade. However, the incorrect invariant is documented in code and the deduplication maps are network-agnostic, creating a latent correctness hazard for any future multi-network extension or state migration.

---

### Likelihood Explanation

The likelihood of collision between Mainnet and Sepolia transaction hashes is non-negligible: any user who sends the same signed transaction (same nonce, recipient, value, gas, signature) to both networks produces identical hashes. This is a known Ethereum cross-chain replay pattern. The practical trigger requires either a state migration across networks or a future code change that processes multiple networks in one minter instance. Given the existing Sepolia testnet minter and Mainnet minter run separately, the immediate risk is low but the architectural flaw is real and the incorrect invariant comment increases the risk of future exploitation.

---

### Recommendation

Include `ethereum_network` (chain ID) in the `EventSource` key to make it network-scoped:

```rust
// rs/ethereum/cketh/minter/src/eth_logs/mod.rs
pub struct EventSource {
    pub chain_id: u64,          // Add: EthereumNetwork::chain_id()
    pub transaction_hash: Hash,
    pub log_index: LogIndex,
}
```

Update `ReceivedEthEvent::source()` and `ReceivedErc20Event::source()` to include the chain ID, sourced from the minter's `ethereum_network` state. Update the comment at line 159 to correctly scope the uniqueness claim to a single chain.

---

### Proof of Concept

1. Deploy ckETH minter on Sepolia (`ethereum_network = Sepolia`, chain_id = 11155111).
2. User sends ETH deposit transaction on Sepolia; minter scrapes the log, records `EventSource { transaction_hash: 0xABCD..., log_index: 5 }` in `minted_events`.
3. The same user (or any user) crafts an identical signed transaction on Ethereum Mainnet (same nonce, recipient, value, gas, signature — possible if the user reuses keys and parameters). The transaction hash is `0xABCD...` on Mainnet as well.
4. If the minter's stable memory is migrated to a Mainnet instance (e.g., disaster recovery), `record_event_to_mint` will find `0xABCD...:5` already in `minted_events` and the assert at line 197 (`assert!(!self.minted_events.contains_key(&event_source))`) will **panic**, causing the minter to trap and permanently block processing of that Mainnet deposit.

Root cause: `EventSource` at [1](#0-0)  does not include the Ethereum chain ID, violating the "globally unique" invariant claimed at [2](#0-1) . The deduplication maps at [3](#0-2)  are keyed by this network-agnostic `EventSource`. The chain ID is available in state via `EthereumNetwork::chain_id()` at [4](#0-3)  but is never incorporated into the deduplication key. The uniqueness assertion at [5](#0-4)  would panic or silently suppress a cross-network collision.

### Citations

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L124-132)
```rust
/// A unique identifier of the event source: the source transaction hash and the log
/// entry index.
#[derive(Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Debug, Decode, Encode)]
pub struct EventSource {
    #[n(0)]
    pub transaction_hash: Hash,
    #[n(1)]
    pub log_index: LogIndex,
}
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L159-162)
```rust
    /// Return event source, which is globally unique regardless of whether
    /// it is for ETH or ERC-20. This is because the `transaction_hash` already
    /// unique determines the transaction, and `log_index` would match the place
    /// in which event appears for this transaction.
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L64-66)
```rust
    pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
    pub minted_events: BTreeMap<EventSource, MintedEvent>,
    pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L191-209)
```rust
    fn record_event_to_mint(&mut self, event: &ReceivedEvent) {
        let event_source = event.source();
        assert!(
            !self.events_to_mint.contains_key(&event_source),
            "there must be no two different events with the same source"
        );
        assert!(!self.minted_events.contains_key(&event_source));
        assert!(!self.invalid_events.contains_key(&event_source));
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

**File:** rs/ethereum/cketh/minter/src/lifecycle.rs (L35-40)
```rust
    pub fn chain_id(&self) -> u64 {
        match self {
            EthereumNetwork::Mainnet => 1,
            EthereumNetwork::Sepolia => 11155111,
        }
    }
```
