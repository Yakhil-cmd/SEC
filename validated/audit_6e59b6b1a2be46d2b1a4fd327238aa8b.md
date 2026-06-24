Audit Report

## Title
Duplicate Verack Resets `AwaitingAddresses` Timeout, Allowing Indefinite Seed Connection Lifetime — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

## Summary
`process_verack_message` dispatches solely on `address_entry()` type with no guard on the current `ConnectionState`. A seed node can send repeated `Verack` messages to continuously reset the `AwaitingAddresses` timestamp, defeating the 5-second seed timeout and keeping the connection alive indefinitely. This gives an attacker-controlled seed an unbounded window to observe adapter query patterns and occupy connection slots in the ckBTC Bitcoin adapter.

## Finding Description
`process_verack_message` at [1](#0-0)  dispatches only on `address_entry()` type, with no check on the current `ConnectionState`. For any seed connection — regardless of whether it is in `Connected`, `AwaitingAddresses`, or any other state — it unconditionally calls `conn.awaiting_addresses()`.

`awaiting_addresses()` at [2](#0-1)  always overwrites `self.state` with a fresh `SystemTime::now()` timestamp.

`flag_seed_addr_retrieval_timeouts()` at [3](#0-2)  computes expiry as `timestamp + SEED_ADDR_RETRIEVED_TIMEOUT_SECS` (5 seconds, [4](#0-3) ) and calls `conn.discard()` only when `expires_at <= now`. Sending one `Verack` every ~4 seconds continuously resets the clock, so `expires_at` never reaches `now` and `discard()` is never called.

The `ConnectionState` enum defines `Initializing`, `Connected`, `HandshakeComplete`, `AwaitingAddresses`, `AdapterDiscarded`, and `NodeDisconnected` at [5](#0-4) , but none of these are checked before the state transition in `process_verack_message`.

Note on the address-poisoning sub-claim: `process_addr_message` at [6](#0-5)  calls `conn.disconnect()` immediately after any addr message from a seed, so a single connection cannot both stay alive and deliver addresses. The address-poisoning vector requires repeated reconnections, which is possible independently of this vulnerability. The confirmed, direct impact of this specific bug is the indefinite connection lifetime enabling an extended observation window and connection-slot occupancy.

## Impact Explanation
The ckBTC Bitcoin adapter is an in-scope Chain Fusion component. An attacker-controlled seed that keeps a connection alive indefinitely can observe all block and header requests issued by the adapter over an unbounded window, revealing the adapter's current chain-tip view. Additionally, holding connection slots open (up to `MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY = 8`) for extended periods can delay or block legitimate seed connections during address discovery, degrading adapter bootstrapping. This constitutes a significant Chain Fusion infrastructure security impact with concrete protocol harm, qualifying as **High** severity.

## Likelihood Explanation
The attacker must control a node whose IP is returned by one of the configured DNS seeds. This is realistic: Bitcoin DNS seeds return IPs of reachable Bitcoin nodes, and an attacker can operate a compliant Bitcoin node to appear in seed responses. No DNS hijacking is required. Once connected, the exploit requires only sending a `Verack` message approximately every 4 seconds, which is trivially implementable with any Bitcoin protocol library.

## Recommendation
Add a state guard in `process_verack_message` so that `awaiting_addresses()` is only called when the connection is in the `Connected` state (i.e., the first legitimate Verack):

```rust
fn process_verack_message(&mut self, address: &SocketAddr) -> Result<(), ProcessNetworkMessageError> {
    if let Ok(conn) = self.get_connection(address) {
        match (conn.address_entry(), conn.state()) {
            (AddressEntry::Seed(_), ConnectionState::Connected { .. }) => {
                conn.awaiting_addresses();
            }
            (AddressEntry::Discovered(_), ConnectionState::Connected { .. }) => {
                conn.completed_handshake();
            }
            _ => {} // ignore duplicate or out-of-order Verack
        }
    }
    Ok(())
}
```

## Proof of Concept
```rust
// Unit test: call process_verack_message twice on a seed connection.
// Assert the second call does NOT update the AwaitingAddresses timestamp.
let t_before = SystemTime::now();
manager.process_verack_message(&seed_addr).unwrap();
let t1 = match conn.state() {
    ConnectionState::AwaitingAddresses { timestamp } => *timestamp,
    _ => panic!("expected AwaitingAddresses"),
};
std::thread::sleep(Duration::from_millis(10));
manager.process_verack_message(&seed_addr).unwrap(); // second Verack
let t2 = match conn.state() {
    ConnectionState::AwaitingAddresses { timestamp } => *timestamp,
    _ => panic!("expected AwaitingAddresses"),
};
assert_eq!(t1, t2, "second Verack must not reset the timestamp");
// With current code: t2 > t1 — assertion fails, confirming the bug.
```

### Citations

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L53-53)
```rust
const SEED_ADDR_RETRIEVED_TIMEOUT_SECS: u64 = 5;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L264-275)
```rust
    fn flag_seed_addr_retrieval_timeouts(&mut self) {
        let now = SystemTime::now();
        for conn in self.connections.values_mut() {
            if let AddressEntry::Seed(_) = *conn.address_entry()
                && let ConnectionState::AwaitingAddresses { timestamp } = *conn.state()
            {
                let expires_at = timestamp + Duration::from_secs(SEED_ADDR_RETRIEVED_TIMEOUT_SECS);
                if expires_at <= now {
                    conn.discard();
                }
            }
        }
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L480-484)
```rust
        if let Ok(conn) = self.get_connection(address) {
            match conn.address_entry() {
                AddressEntry::Seed(_) => conn.awaiting_addresses(),
                AddressEntry::Discovered(_) => conn.completed_handshake(),
            };
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L557-561)
```rust
        if let Ok(conn) = self.get_connection(address)
            && let AddressEntry::Seed(_) = conn.address_entry()
        {
            conn.disconnect();
        }
```

**File:** rs/bitcoin/adapter/src/connection.rs (L39-59)
```rust
pub enum ConnectionState {
    /// This variant represents that the connection has not yet been connected.
    Initializing,
    /// This variant represents that the connection is now connected with the stream.
    Connected {
        /// This field represents when the connection state was changed to this value.
        timestamp: SystemTime,
    },
    /// This variant represents that the version handshake has been completed.
    HandshakeComplete,
    /// This variant represents that the adapter has discarded the connection
    /// due to bad behavior.
    AdapterDiscarded,
    /// This variant represents that the connection has been dropped.
    NodeDisconnected,
    /// The connection has sent a `getaddr` message and is now waiting for a response.
    AwaitingAddresses {
        /// The timestamp when the state change occurred.
        timestamp: SystemTime,
    },
}
```

**File:** rs/bitcoin/adapter/src/connection.rs (L181-185)
```rust
    pub fn awaiting_addresses(&mut self) {
        self.state = ConnectionState::AwaitingAddresses {
            timestamp: SystemTime::now(),
        };
    }
```
