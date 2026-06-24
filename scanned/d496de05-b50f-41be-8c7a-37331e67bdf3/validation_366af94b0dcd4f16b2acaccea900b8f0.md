### Title
Unsolicited Pong Causes Immediate Connection Termination — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

### Summary

`process_pong_message` unconditionally calls `conn.disconnect()` when a `Pong` is received while the connection is in `PingState::Idle`. Any Bitcoin peer the adapter connects to can send an unsolicited `Pong` at any time to force the adapter to drop the connection. Because `disconnect()` (not `discard()`) is used, the peer's address is returned to `known_addresses` and the adapter may reconnect to the same malicious peer, creating a persistent disruption loop.

### Finding Description

In `connectionmanager.rs`, `process_pong_message` evaluates whether a received `Pong` is valid: [1](#0-0) 

When the connection is in `PingState::Idle` (no outstanding ping), `valid_pong` is set to `false` and `conn.disconnect()` is called unconditionally. There is no distinction between "wrong nonce on an expected pong" (genuine misbehavior) and "pong arrived when none was expected" (which the Bitcoin P2P protocol does not prohibit and which honest nodes may send).

`conn.disconnect()` sets the state to `NodeDisconnected` and aborts the stream task: [2](#0-1) 

In `reap_disconnected`, a `NodeDisconnected` connection causes the address to be moved back into `known_addresses` (not removed from the book): [3](#0-2) 

This is the critical difference from `discard()`, which removes the address entirely. After reaping, `manage_connections` immediately tries to refill connections: [4](#0-3) 

The adapter may reconnect to the same malicious peer, which can again send an unsolicited `Pong`, creating a persistent disconnect-reconnect loop for that connection slot.

### Impact Explanation

The adapter maintains between 2 (`min_connections`) and 5 (`max_connections`) connections: [5](#0-4) 

If 2 or more peers in the adapter's address book are malicious and continuously send unsolicited `Pong` messages, the adapter can be kept below `min_connections` for extended periods, stalling block delivery to the ckBTC canister. Each malicious peer occupies a connection slot in a tight disconnect-reconnect cycle, preventing honest peers from being used for block fetching.

### Likelihood Explanation

The attacker must control Bitcoin peers that the adapter connects to. This is a realistic threat model: the adapter discovers peers via DNS seeds and `addr` messages, and a malicious operator can run Bitcoin nodes that appear legitimate until after the handshake. The exploit requires no privileged access — only the ability to run a Bitcoin node that completes the version handshake and then sends a `Pong` message. The Bitcoin P2P protocol places no restriction on sending `Pong` at any time.

### Recommendation

Replace the `conn.disconnect()` in the `PingState::Idle` branch with a no-op (log and ignore). An unsolicited `Pong` is not a protocol violation warranting disconnection; only a `Pong` with a mismatched nonce (when one was expected) indicates misbehavior. The fix:

```rust
PingState::Idle { .. } => {
    // Unsolicited pong — ignore silently or log a warning.
    trace!(self.logger, "Received unsolicited pong from {}", address);
    return Ok(());
}
```

Alternatively, only disconnect when `PingState::ExpectingPong` and the nonce does not match.

### Proof of Concept

1. Establish a connection and complete the version handshake → `ConnectionState::HandshakeComplete`, `PingState::Idle`.
2. Send `NetworkMessage::Pong(42)` from the peer side (any nonce).
3. `process_pong_message` is called → `valid_pong = false` (Idle branch) → `conn.disconnect()`.
4. `reap_disconnected` removes the connection but returns the address to `known_addresses`.
5. `manage_connections` reconnects to the same peer.
6. Repeat from step 2 indefinitely.

The connection slot is permanently occupied in a disconnect-reconnect cycle, and with 2+ such peers the adapter stays below `min_connections`, stalling ckBTC block ingestion.

### Citations

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L211-213)
```rust
        while self.connections.len() < self.get_max_number_of_connections() {
            self.make_connection(handle).await?;
        }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L286-288)
```rust
                ConnectionState::NodeDisconnected => {
                    self.address_book.remove_from_active(conn.address_entry());
                }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L517-531)
```rust
            let valid_pong = match conn.ping_state() {
                PingState::ExpectingPong {
                    ping_sent_at: _,
                    nonce: ping_nonce,
                } => *ping_nonce == nonce,
                PingState::Idle { last_pong_at: _ } => false,
            };

            if valid_pong {
                conn.idle();
            } else {
                // Received an unexpected or invalid `pong` message.
                // Disconnect from the BTC node.
                conn.disconnect();
            };
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L703-710)
```rust
fn connection_limits(address_book: &AddressBook) -> (usize, usize) {
    if address_book.has_seeds() {
        // Seeds are available.
        (2, 5)
    } else {
        // No seeds are available. Can only connect to nodes explicitly provided.
        (address_book.size(), address_book.size())
    }
```

**File:** rs/bitcoin/adapter/src/connection.rs (L189-192)
```rust
    pub fn disconnect(&mut self) {
        self.state = ConnectionState::NodeDisconnected;
        self.handle.abort();
    }
```
