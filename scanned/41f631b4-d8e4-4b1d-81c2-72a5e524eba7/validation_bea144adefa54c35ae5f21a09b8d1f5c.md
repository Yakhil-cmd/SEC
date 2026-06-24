I have all the code needed to fully trace the claimed exploit path. Let me analyze each step precisely.

The complete call chain is confirmed in the code. Here is my analysis:

---

### Title
Single `NetworkMessage::Unknown` Permanently Removes Peer Address, Enabling Address Book Depletion — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

### Summary

`process_unknown_message` unconditionally calls `internal_discard` on any peer that sends a single unknown Bitcoin P2P message. This triggers a permanent removal of that peer's address from the address book (when DNS seeds are configured), with no rate limiting, counter, or grace period. An attacker controlling enough Bitcoin peers can deplete the address book and force the adapter into a continuous re-discovery loop, disrupting Bitcoin integration.

### Finding Description

The full call chain is confirmed in production code:

**Step 1** — Router dispatches any received `NetworkMessage::Unknown` to `process_bitcoin_network_message`: [1](#0-0) 

**Step 2** — `process_bitcoin_network_message` matches `Unknown` and calls `process_unknown_message`: [2](#0-1) 

**Step 3** — `process_unknown_message` unconditionally calls `internal_discard` after logging. No counter, no threshold, no grace period: [3](#0-2) 

**Step 4** — `internal_discard` calls `conn.discard()`, setting state to `AdapterDiscarded`: [4](#0-3) [5](#0-4) 

**Step 5** — On the next `tick()`, `reap_disconnected` sees `AdapterDiscarded` and calls `address_book.discard()`: [6](#0-5) 

**Step 6** — `address_book.discard()` permanently removes the address from both `active_addresses` and `known_addresses` when `has_seeds()` is true (i.e., DNS seeds are configured, which is the normal production deployment): [7](#0-6) 

There is no state check gating `process_unknown_message` — it fires for connections in any `ConnectionState`, not just `HandshakeComplete`. A peer does not even need to complete the handshake to trigger this path.

### Impact Explanation

When the address book is depleted, `make_connection` falls back to `resolve_next_seed()`, which rebuilds from DNS seeds. [8](#0-7) 

This means the adapter is not permanently bricked — it can rediscover peers. However, the attacker can sustain the attack by continuously getting new addresses into the adapter's address book (via the normal `addr` message propagation mechanism) and then sending `Unknown` from each. During the re-discovery phase, the adapter is limited to `MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY = 8` connections and cannot serve Bitcoin data to the IC. [9](#0-8) 

The practical impact is disruption of the IC's Bitcoin integration (ckBTC, canister Bitcoin API calls) for as long as the attacker sustains the campaign.

### Likelihood Explanation

Running Bitcoin nodes is unprivileged. An attacker can:
1. Spin up nodes that speak the Bitcoin P2P handshake correctly.
2. Wait for their addresses to propagate into the adapter's address book via `addr` messages from other peers.
3. Accept a connection from the adapter, then immediately send a message with an unknown command string.

No admin access, no key material, no governance majority, and no BGP/DNS hijack is required. The only resource cost is running enough Bitcoin-protocol-speaking nodes.

### Recommendation

- Add a misbehavior counter per address. Only call `internal_discard` after N unknown messages within a time window, not on the first occurrence.
- Alternatively, treat `Unknown` messages as a soft disconnect (`conn.disconnect()` → `NodeDisconnected` → `remove_from_active`, which returns the address to `known_addresses`) rather than a hard discard.
- Add a minimum address book floor: if `known_addresses` would drop below `min_addresses`, do not permanently discard — demote to `NodeDisconnected` instead.

### Proof of Concept

State-machine test (no network required):
1. Create a `ConnectionManager` with DNS seeds configured.
2. Manually insert 10 `AddressEntry::Discovered` connections in `HandshakeComplete` state into `manager.connections` and their addresses into `address_book.active_addresses`.
3. Call `manager.process_bitcoin_network_message(addr_i, &NetworkMessage::Unknown { command: "exploit".parse().unwrap(), payload: vec![] })` for each of the 10 addresses.
4. Call `manager.reap_disconnected()`.
5. Assert `manager.address_book.size() == 0` — all addresses have been permanently removed.

### Citations

**File:** rs/bitcoin/adapter/src/router.rs (L96-99)
```rust
                    if let Err(ProcessNetworkMessageError::InvalidMessage) =
                        connection_manager.process_bitcoin_network_message(address, &message) {
                        connection_manager.discard(&address);
                    }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L56-56)
```rust
const MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY: usize = 8;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L157-161)
```rust
    fn internal_discard(&mut self, address: &SocketAddr) {
        if let Ok(conn) = self.get_connection(address) {
            conn.discard();
        }
    }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L279-295)
```rust
    fn reap_disconnected(&mut self) {
        let mut disconnects = vec![];
        for (addr, conn) in self.connections.iter() {
            match conn.state() {
                ConnectionState::AdapterDiscarded => {
                    self.address_book.discard(conn.address_entry());
                }
                ConnectionState::NodeDisconnected => {
                    self.address_book.remove_from_active(conn.address_entry());
                }
                _ => {}
            }

            if conn.is_disconnected() {
                disconnects.push(*addr);
            }
        }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L308-313)
```rust
        let address_entry_result = if !self.address_book.has_enough_addresses() {
            self.address_book.resolve_next_seed().await
        } else {
            self.address_book.pop()
        };
        let address_entry = address_entry_result.map_err(ConnectionManagerError::AddressBook)?;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L577-594)
```rust
    fn process_unknown_message(
        &mut self,
        address: &SocketAddr,
        command: &CommandString,
        payload: &[u8],
    ) -> Result<(), ProcessNetworkMessageError> {
        // If we receive an unknown message from a BTC node, the adapter should log
        // the message for further analysis.
        warn!(
            self.logger,
            "Received an unknown message from {}, command: {}, payload: {}",
            address,
            command,
            hex::encode(payload),
        );
        self.internal_discard(address);
        Ok(())
    }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L695-697)
```rust
            NetworkMessage::Unknown { command, payload } => {
                self.process_unknown_message(&address, command, payload)
            }
```

**File:** rs/bitcoin/adapter/src/connection.rs (L197-200)
```rust
    pub fn discard(&mut self) {
        self.state = ConnectionState::AdapterDiscarded;
        self.handle.abort();
    }
```

**File:** rs/bitcoin/adapter/src/addressbook.rs (L292-301)
```rust
    pub fn discard(&mut self, address: &AddressEntry) {
        if let AddressEntry::Discovered(addr) = address {
            if self.has_seeds() {
                self.active_addresses.remove(addr);
                self.known_addresses.remove(addr);
            } else {
                self.remove_from_active(address);
            }
        }
    }
```
