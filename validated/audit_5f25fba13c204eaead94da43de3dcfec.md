Audit Report

## Title
Duplicate Verack Resets `AwaitingAddresses` Timeout, Allowing Indefinite Seed Connection Lifetime — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

## Summary
`process_verack_message` dispatches solely on `address_entry()` type with no guard on the current `ConnectionState`. A seed node can send repeated `Verack` messages to continuously reset the `AwaitingAddresses` timestamp, defeating the 5-second seed timeout. This allows an attacker-controlled seed to occupy adapter connection slots indefinitely and extend its observation window of adapter query patterns.

## Finding Description
`process_verack_message` (L480–484) matches only on `AddressEntry` type, with no check on the current `ConnectionState`:

```rust
// connectionmanager.rs:480-484
if let Ok(conn) = self.get_connection(address) {
    match conn.address_entry() {
        AddressEntry::Seed(_) => conn.awaiting_addresses(),   // no state guard
        AddressEntry::Discovered(_) => conn.completed_handshake(),
    };
}
``` [1](#0-0) 

`awaiting_addresses()` unconditionally overwrites the state with a fresh wall-clock timestamp:

```rust
// connection.rs:181-185
pub fn awaiting_addresses(&mut self) {
    self.state = ConnectionState::AwaitingAddresses {
        timestamp: SystemTime::now(),   // always reset
    };
}
``` [2](#0-1) 

`flag_seed_addr_retrieval_timeouts()` computes expiry from that stored timestamp using `SEED_ADDR_RETRIEVED_TIMEOUT_SECS = 5`:

```rust
// connectionmanager.rs:270-272
let expires_at = timestamp + Duration::from_secs(SEED_ADDR_RETRIEVED_TIMEOUT_SECS);
if expires_at <= now { conn.discard(); }
``` [3](#0-2) [4](#0-3) 

Sending one `Verack` every ~4 seconds keeps the connection in `AwaitingAddresses` state indefinitely. Note: the submitted claim's assertion about "repeated small batches" of addresses is incorrect — `process_addr_message` calls `conn.disconnect()` immediately after any `addr` message from a seed (L557–561), so only one address batch can be sent per connection lifetime. The actual exploitable impact is connection slot occupation and extended query-pattern observation. [5](#0-4) 

## Impact Explanation
During address discovery, the adapter allows at most `MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY = 8` simultaneous connections. [6](#0-5) 

An attacker controlling nodes returned by DNS seeds can occupy one or more of these 8 slots indefinitely. If enough slots are held, the adapter cannot complete address discovery and cannot establish connections to legitimate Bitcoin nodes. This directly impairs ckBTC's ability to observe the Bitcoin chain tip, constituting a concrete Chain Fusion availability impact. Additionally, the extended connection lifetime allows the attacker to observe which block/header requests the adapter issues, leaking information about its chain-sync state. This maps to **High** ($2,000–$10,000): significant Chain Fusion security impact with concrete protocol harm.

## Likelihood Explanation
The attacker must run a Bitcoin node that appears in a DNS seed response — a realistic precondition requiring no DNS hijacking, only operating a compliant Bitcoin node. Sending periodic `Verack` messages is trivially implementable. Filling all 8 slots requires controlling multiple seed-listed nodes, which raises the bar, but even partial slot occupation degrades address discovery. The attack is repeatable and requires no victim interaction.

## Recommendation
Add a state guard in `process_verack_message` so that `awaiting_addresses()` is only called when the connection is in `Connected` or `HandshakeComplete` state (i.e., the first legitimate Verack):

```rust
fn process_verack_message(&mut self, address: &SocketAddr) -> Result<(), ProcessNetworkMessageError> {
    if let Ok(conn) = self.get_connection(address) {
        match (conn.address_entry(), conn.state()) {
            (AddressEntry::Seed(_), ConnectionState::Connected { .. })
            | (AddressEntry::Seed(_), ConnectionState::HandshakeComplete) => {
                conn.awaiting_addresses();
            }
            (AddressEntry::Discovered(_), ConnectionState::Connected { .. }) => {
                conn.completed_handshake();
            }
            _ => {} // ignore duplicate Verack
        }
    }
    Ok(())
}
```

## Proof of Concept
```rust
// Unit test: call process_verack_message twice on a seed connection.
// Assert the second call does NOT update the timestamp.
manager.process_verack_message(&seed_addr).unwrap();
let t1 = match conn.state() {
    ConnectionState::AwaitingAddresses { timestamp } => *timestamp,
    _ => panic!(),
};
std::thread::sleep(Duration::from_millis(10));
manager.process_verack_message(&seed_addr).unwrap(); // second Verack
let t2 = match conn.state() {
    ConnectionState::AwaitingAddresses { timestamp } => *timestamp,
    _ => panic!(),
};
assert_eq!(t1, t2, "second Verack must not reset the timestamp");
// With current code: t2 > t1 — assertion fails, confirming the bug.
```

### Citations

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L53-53)
```rust
const SEED_ADDR_RETRIEVED_TIMEOUT_SECS: u64 = 5;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L55-56)
```rust
/// This constant represents how many connections can be made during the address discovery process.
const MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY: usize = 8;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L270-272)
```rust
                let expires_at = timestamp + Duration::from_secs(SEED_ADDR_RETRIEVED_TIMEOUT_SECS);
                if expires_at <= now {
                    conn.discard();
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

**File:** rs/bitcoin/adapter/src/connection.rs (L181-185)
```rust
    pub fn awaiting_addresses(&mut self) {
        self.state = ConnectionState::AwaitingAddresses {
            timestamp: SystemTime::now(),
        };
    }
```
