Audit Report

## Title
Duplicate `Verack` Resets `AwaitingAddresses` Timeout, Allowing Indefinite Seed Connection Lifetime — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

## Summary
`process_verack_message` dispatches solely on `address_entry()` type with no guard on the current `ConnectionState`. A seed node can send repeated `Verack` messages to continuously reset the `AwaitingAddresses` timestamp, defeating the 5-second `SEED_ADDR_RETRIEVED_TIMEOUT_SECS` guard and keeping the connection alive indefinitely. This allows an attacker-controlled seed to occupy a connection slot without bound and observe adapter query patterns for an extended period.

## Finding Description
`process_verack_message` at [1](#0-0)  dispatches only on `address_entry()` type, with no check on the current `ConnectionState`. For a `Seed` entry it unconditionally calls `conn.awaiting_addresses()`.

`awaiting_addresses()` at [2](#0-1)  unconditionally overwrites `self.state` with a fresh `SystemTime::now()` timestamp on every call, regardless of whether the connection is already in `AwaitingAddresses`.

`flag_seed_addr_retrieval_timeouts()` at [3](#0-2)  computes expiry as `timestamp + Duration::from_secs(SEED_ADDR_RETRIEVED_TIMEOUT_SECS)` where `SEED_ADDR_RETRIEVED_TIMEOUT_SECS = 5` at [4](#0-3) . Sending one `Verack` every ~4 seconds continuously refreshes the timestamp, so `expires_at` never falls behind `now` and `conn.discard()` is never called.

Regarding the "repeated small batches" address-poisoning sub-claim: `add_many` at [5](#0-4)  checks `addresses.len() > MAX_ADDR_MESSAGE_SIZE` before adding anything, and `process_addr_message` at [6](#0-5)  calls `conn.disconnect()` immediately after every successful addr message from a seed. Therefore the Verack reset does not enable sending multiple address batches on a single connection — the connection is torn down after the first valid `addr`. The address-poisoning-via-repeated-batches impact is overstated; the confirmed impacts are (1) indefinite connection-slot occupation and (2) extended observation of adapter query patterns.

## Impact Explanation
The adapter limits address-discovery connections to `MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY = 8` at [7](#0-6) . An attacker controlling seed IPs that appear in DNS seed responses can hold slots open indefinitely, starving legitimate seeds of connection opportunities and delaying or blocking address discovery. This constitutes a constrained availability impact on the ckBTC Bitcoin adapter's address-discovery phase, a Chain Fusion component. Severity: **Medium** — the attack requires node/boundary-node-level control (a node listed in DNS seed responses) and meaningful per-slot effort, with a concrete but bounded impact on adapter availability and query-pattern confidentiality.

## Likelihood Explanation
The attacker must operate a Bitcoin node whose IP is returned by one of the configured DNS seeds. This is realistic: Bitcoin DNS seeds return IPs of reachable nodes, and an attacker can run a standards-compliant node to be listed. No DNS hijacking is required. Once connected, the exploit is trivial: send a `Verack` message every ~4 seconds. Occupying all 8 discovery slots requires controlling 8 such IPs, which raises the bar but remains within reach of a motivated attacker.

## Recommendation
Add a `ConnectionState` guard in `process_verack_message` so that `awaiting_addresses()` is only called on the first `Verack` (i.e., when the connection is in `Connected` state):

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
// With current code t2 > t1 — assertion fails, confirming the bug.
```

### Citations

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L53-53)
```rust
const SEED_ADDR_RETRIEVED_TIMEOUT_SECS: u64 = 5;
```

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L56-56)
```rust
const MAX_CONNECTIONS_DURING_ADDRESS_DISCOVERY: usize = 8;
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

**File:** rs/bitcoin/adapter/src/connection.rs (L181-185)
```rust
    pub fn awaiting_addresses(&mut self) {
        self.state = ConnectionState::AwaitingAddresses {
            timestamp: SystemTime::now(),
        };
    }
```

**File:** rs/bitcoin/adapter/src/addressbook.rs (L187-192)
```rust
        if addresses.len() > MAX_ADDR_MESSAGE_SIZE {
            return Err(AddressBookError::TooManyAddresses {
                received: addresses.len(),
                max_amount: MAX_ADDR_MESSAGE_SIZE,
            });
        }
```
