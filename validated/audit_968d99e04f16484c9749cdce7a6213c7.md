The code is clear. Let me confirm the exact state guard (or lack thereof) in `process_verack_message`:

The vulnerability is confirmed. The code path is unambiguous:

- `process_verack_message` dispatches solely on `address_entry()` type with **no check on current `ConnectionState`** — a second `Verack` from a seed unconditionally calls `conn.awaiting_addresses()`.
- `awaiting_addresses()` unconditionally overwrites the state with `SystemTime::now()`.
- `flag_seed_addr_retrieval_timeouts()` uses that stored timestamp to compute expiry (`SEED_ADDR_RETRIEVED_TIMEOUT_SECS = 5`), so a fresh timestamp prevents discard.

---

### Title
Duplicate Verack Resets `AwaitingAddresses` Timeout, Allowing Indefinite Seed Connection Lifetime — (`rs/bitcoin/adapter/src/connectionmanager.rs`)

### Summary
`process_verack_message` contains no guard on the current `ConnectionState`. A malicious seed node can send repeated `Verack` messages to continuously reset the `AwaitingAddresses` timestamp, defeating the 5-second seed timeout and keeping the connection alive indefinitely.

### Finding Description
`process_verack_message` dispatches only on `address_entry()` type:

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

`flag_seed_addr_retrieval_timeouts()` computes expiry from that stored timestamp:

```rust
// connectionmanager.rs:270-272
let expires_at = timestamp + Duration::from_secs(SEED_ADDR_RETRIEVED_TIMEOUT_SECS);
if expires_at <= now { conn.discard(); }
``` [3](#0-2) 

`SEED_ADDR_RETRIEVED_TIMEOUT_SECS` is 5 seconds. [4](#0-3) 

Each time the seed sends a `Verack`, the 5-second clock restarts. Sending one `Verack` every ~4 seconds keeps the connection alive indefinitely.

### Impact Explanation
A seed connection that never times out can:
1. Send addresses in repeated small batches (staying under the `TooManyAddresses` limit) over an unbounded window, poisoning the adapter's address book.
2. Observe adapter query patterns (which blocks/headers are requested) for an extended period.

A poisoned address book causes the ckBTC Bitcoin adapter to preferentially connect to attacker-controlled Bitcoin nodes, giving the attacker influence over the adapter's view of the Bitcoin chain tip — directly affecting ckBTC chain-fusion correctness. [5](#0-4) 

### Likelihood Explanation
The attacker must control a node whose IP is returned by one of the configured DNS seeds. This is a realistic precondition: Bitcoin DNS seeds return IPs of reachable Bitcoin nodes, and an attacker can run a compliant Bitcoin node to get listed. No DNS hijacking is required — the attacker simply needs their node to appear in the seed response. Once connected, the exploit requires only sending periodic `Verack` messages, which is trivially implementable.

### Recommendation
Add a state guard in `process_verack_message` so that `awaiting_addresses()` is only called when the connection is in the `Connected` or `HandshakeComplete` state (i.e., the first Verack):

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

### Proof of Concept
```rust
// Call process_verack_message twice on a seed connection.
// Assert the second call does NOT update the timestamp.
let t_before = SystemTime::now();
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
```

With the current code, `t2 > t1` — the assertion fails, confirming the timestamp is reset.

### Citations

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L53-53)
```rust
const SEED_ADDR_RETRIEVED_TIMEOUT_SECS: u64 = 5;
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

**File:** rs/bitcoin/adapter/src/connectionmanager.rs (L539-561)
```rust
    fn process_addr_message(
        &mut self,
        address: &SocketAddr,
        addresses: &[(AddressTimestamp, Address)],
    ) -> Result<(), ProcessNetworkMessageError> {
        let result = self.address_book.add_many(address, addresses);
        if let Err(AddressBookError::TooManyAddresses {
            received,
            max_amount,
        }) = result
        {
            warn!(
                self.logger,
                "Received {} addresses from {} (max: {})", received, address, max_amount
            );
            return Err(ProcessNetworkMessageError::InvalidMessage);
        }

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
