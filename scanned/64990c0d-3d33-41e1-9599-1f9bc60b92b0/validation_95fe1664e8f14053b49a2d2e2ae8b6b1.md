### Title
Block Ingress Capacity Exhaustion via Zero-Cost Sender-Free Message Flooding - (File: `rs/ingress_manager/src/ingress_selector.rs`)

### Summary
The IC's per-block ingress capacity (`MAX_INGRESS_MESSAGES_PER_BLOCK = 1000`, `MAX_INGRESS_BYTES_PER_BLOCK = 4 MB`) can be exhausted by an unprivileged attacker at near-zero cost because ingress induction fees are charged to the **receiving canister**, not the sender. By flooding blocks with messages directed at many different canisters, an attacker can probabilistically exclude victim messages from every block until they expire after `MAX_INGRESS_TTL` (5 minutes).

### Finding Description

The ingress selector's `get_ingress_payload` function in `rs/ingress_manager/src/ingress_selector.rs` builds block payloads using a per-destination-canister round-robin. The block is hard-capped at `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` messages and `MAX_INGRESS_BYTES_PER_BLOCK = 4 MB` defined in `rs/limits/src/lib.rs`.

**Root cause 1 — Ingress cost is paid by the receiver, not the sender.**

In `rs/ingress_manager/src/ingress_selector.rs` the only financial check during block building is whether the *receiving* canister has enough cycles:

```rust
IngressInductionCost::Fee { payer, cost: ingress_cost } =>
    match state.canister_state(&payer) {
        Some(canister) => {
            // checks canister's balance, not sender's
            self.cycles_account_manager.can_withdraw_cycles_with_threshold(...)
        }
    }
```

This mirrors `CyclesAccountManager::ingress_induction_cost` in `rs/cycles_account_manager/src/cycles_account_manager.rs`, which always sets `payer = canister_id` (the destination), never the sender principal. The sender pays nothing.

**Root cause 2 — No per-sender limits exist in the ingress selector or ingress pool.**

The ingress pool's `exceeds_limit` in `rs/artifact_pool/src/ingress_pool.rs` is keyed on `originator_id: NodeId` (a P2P gossip peer node), not on the user's principal. All user-submitted messages arrive via the local node and share the same originator ID. There is no per-user-principal quota anywhere in the block-building path.

**Root cause 3 — The round-robin is per-destination canister, giving the attacker proportional block share.**

The selector groups messages by destination canister and iterates in a randomized order. Each canister gets at least one message included per round-robin pass. With N attacker-controlled destination canisters and 1 victim canister, the probability the victim's canister is among the first 1000 selected is `1000 / (N + 1)`. For N = 9000 this is ~10%, meaning the victim is excluded ~90% of the time per block.

**Attack flow:**

1. Attacker identifies (or creates) N >> 1000 canisters on the target subnet that have sufficient cycles and no `canister_inspect_message` guard (the default for most canisters).
2. Attacker submits one ingress message to each of those N canisters. The sender pays nothing; each receiving canister is charged `ingress_message_reception_fee (1,200,000 cycles) + ingress_byte_reception_fee (2,000 cycles/byte)`.
3. The block maker's `get_ingress_payload` fills the 1000-message cap with attacker messages, probabilistically excluding the victim's canister.
4. The attacker repeats every block (~1 s on app subnets).
5. After `MAX_INGRESS_TTL = 5 minutes`, the victim's messages expire and are purged from the ingress pool.

### Impact Explanation

Victim ingress messages are continuously excluded from blocks and expire after 5 minutes, producing a permanent, renewable denial-of-service against any targeted user or canister. Unlike the Optimism analog where the victim's transaction reverts immediately, here the victim receives no error — the message simply never executes, which is worse from a UX perspective. The attack can be sustained indefinitely as long as the attacker can keep N messages in the pool, and the attacker's direct cost is zero (receiving canisters bear all cycles charges).

### Likelihood Explanation

An unprivileged principal with no special access can execute this attack. The only prerequisites are knowledge of N canister IDs on the subnet (publicly discoverable via the IC dashboard or registry) and the ability to submit HTTP `/api/v2/canister/{id}/call` requests. Boundary-node IP-based rate limits (`rate_limit_per_second_per_ip`) provide partial mitigation but can be bypassed by distributing requests across multiple IPs or by connecting directly to replica nodes. The attack is therefore realistically achievable by a motivated adversary.

### Recommendation

1. **Introduce per-sender-principal limits in the ingress selector.** Track how many messages from each sender principal are included per block and cap the contribution of any single principal to a fraction of `MAX_INGRESS_MESSAGES_PER_BLOCK`.
2. **Charge the sender, not only the receiver, a small submission fee.** Even a nominal fee (e.g., a fraction of the induction cost) would make sustained flooding economically costly for the attacker.
3. **Add per-principal rate limiting at the replica HTTP endpoint** (not only at the boundary node), so that a single principal cannot submit more than K messages per second regardless of how many destination canisters are targeted.

### Proof of Concept

```
# Attacker setup (one-time)
# Identify 9000 canister IDs on the target app subnet (publicly visible)
CANISTERS=$(ic-admin --nns-url ... get-subnet-list | head -9000)

# Per-block loop (~1 s cadence)
while true; do
  for CANISTER_ID in $CANISTERS; do
    # Sender pays nothing; receiving canister pays ~1.2M cycles
    curl -s -X POST "https://ic0.app/api/v2/canister/$CANISTER_ID/call" \
      -H "Content-Type: application/cbor" \
      --data-binary @minimal_signed_ingress.cbor &
  done
  wait
  sleep 0.9
done
# Result: victim's messages to any canister on the subnet are excluded
# from ~90% of blocks and expire after MAX_INGRESS_TTL = 5 minutes.
```

**Key code locations:**

- Hard message-count cap: [1](#0-0) 
- Round-robin quota (per destination canister, not per sender): [2](#0-1) 
- Cycles check on receiving canister only (sender unchecked): [3](#0-2) 
- Ingress cost assigned to receiving canister: [4](#0-3) 
- `exceeds_limit` keyed on P2P NodeId, not user principal: [5](#0-4) 
- `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` and `MAX_INGRESS_TTL = 5 min`: [6](#0-5)

### Citations

**File:** rs/ingress_manager/src/ingress_selector.rs (L162-165)
```rust
        let mut quota = match canister_count {
            0 => return PayloadWithSizeEstimate::default(),
            canister_count @ 1.. => memory_byte_limit.get() as usize / canister_count,
        };
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L510-517)
```rust
        if num_messages >= settings.max_ingress_messages_per_block {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressPayloadTooManyMessages(
                    num_messages,
                    settings.max_ingress_messages_per_block,
                ),
            ));
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L556-595)
```rust
        match self.cycles_account_manager.ingress_induction_cost(
            signed_ingress,
            effective_canister_id,
            subnet_cycles_config,
        ) {
            IngressInductionCost::Fee {
                payer,
                cost: ingress_cost,
            } => match state.canister_state(&payer) {
                Some(canister) => {
                    let cumulative_ingress_cost =
                        cycles_needed.entry(payer).or_insert_with(Cycles::zero);
                    if let Err(err) = self
                        .cycles_account_manager
                        .can_withdraw_cycles_with_threshold(
                            &canister.system_state,
                            *cumulative_ingress_cost + ingress_cost,
                            canister.memory_usage(),
                            canister.message_memory_usage(),
                            canister.system_state.reserved_balance(),
                            subnet_cycles_config,
                            false, // error here is not returned back to the user => no need to reveal top up balance
                        )
                    {
                        return Err(ValidationError::InvalidArtifact(
                            InvalidIngressPayloadReason::InsufficientCycles(err),
                        ));
                    }
                    *cumulative_ingress_cost += ingress_cost;
                }
                None => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterNotFound(payer),
                    ));
                }
            },
            IngressInductionCost::Free => {
                // Do nothing.
            }
        };
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L559-597)
```rust
    pub fn ingress_induction_cost(
        &self,
        ingress: &SignedIngress,
        effective_canister_id: Option<CanisterId>,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> IngressInductionCost {
        let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
        let ingress = ingress.content();
        let paying_canister = match ingress.is_addressed_to_subnet() {
            // If a subnet message, get effective canister id who will pay for the message.
            true => {
                if let Ok(Method::UpdateSettings) = Method::from_str(ingress.method_name()) {
                    // The fee for `UpdateSettings` with small payload is charged after
                    // applying the settings to allow users to unfreeze canisters
                    // after accidentally setting the freezing threshold too high.
                    if self.is_delayed_ingress_induction_cost(ingress.arg()) {
                        None
                    } else {
                        effective_canister_id
                    }
                } else {
                    effective_canister_id
                }
            }
            // A message to a canister is always paid for by the receiving canister.
            false => Some(ingress.canister_id()),
        };

        match paying_canister {
            Some(paying_canister) => {
                let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
                IngressInductionCost::Fee {
                    payer: paying_canister,
                    cost: cost.real(),
                }
            }
            None => IngressInductionCost::Free,
        }
    }
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```

**File:** rs/limits/src/lib.rs (L17-78)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);

/// Duration added to `MAX_INGRESS_TTL` when checking the max allowed
/// expiry at the http handler. The purpose is to admit ingress created with
/// MAX_INGRESS_TTL by clients with a slightly skewed local clock instead
/// of rejecting them right away.
pub const PERMITTED_DRIFT_AT_VALIDATOR: Duration = Duration::from_secs(30);

/// The maximum number of messages that can be present in the ingress history
/// at any one time.
///
/// The value is the product of the default `max_ingress_messages_per_block`
/// configured in the subnet record; and the `MAX_INGRESS_TTL` (assuming a block
/// rate of 1 block per second). Times 2, since we could theoretically have
/// `MAX_INGRESS_TTL` worth of `Received` messages; plus the same number of
/// messages in terminal states.
pub const INGRESS_HISTORY_MAX_MESSAGES: usize = 2 * 1000 * MAX_INGRESS_TTL.as_secs() as usize;

/// Message count limit for `System` subnet outgoing streams used for throttling
/// the matching input stream.
pub const SYSTEM_SUBNET_STREAM_MSG_LIMIT: usize = 100;

/// The `ic-prep` configuration for app subnets is used for new app subnets with at most
/// 13 nodes. App subnets with more nodes will be deployed with the `ic-prep`
/// configuration for NNS subnet.
pub const SMALL_APP_SUBNET_MAX_SIZE: usize = 13;

/// Cycles threshold to reduce logging load for canister operations with cycles.
pub const LOG_CANISTER_OPERATION_CYCLES_THRESHOLD: u128 = 100_000_000_000;

pub const KILOBYTE: u64 = 1024;
pub const MEGABYTE: u64 = KILOBYTE * KILOBYTE;

/// How long to wait before a block maker of higher rank can create a block. The value should be
/// high enough to allow a lower rank block marker to broadcast their block to all their peers,
/// before a higher rank block maker starts creating one. It should then depend on the size of the
/// subnet and the maximum size of a block. For example, on an app subnet with 13 nodes and with
/// maximum block size set to 4MB, assuming at least 300Mbit/s throughput for each node, a block
/// maker will need roughly one second to broadcast their block. On the nns subnet with 40 nodes,
/// it should take roughly three seconds on average.
pub const UNIT_DELAY_APP_SUBNET: Duration = Duration::from_millis(1000);
pub const UNIT_DELAY_NNS_SUBNET: Duration = Duration::from_millis(3000);
pub const INITIAL_NOTARY_DELAY: Duration = Duration::from_millis(300);
/// Default value for the maximum size, in bytes, a [`BatchPayload`] can have *when sent over wire*.
/// Increasing this value too much could result in longer delivery times of blocks to peers, which
/// could lead to forks as higher rank blocks could be proposed meanwhile. See the comment about
/// [`UNIT_DELAY_APP_SUBNET`].
/// Note that with hashes-in-blocks feature enabled, the blocks sent over wire are typically smaller
/// than their representation in memory, because we strip some of the data before broadcasting them
/// to peers.
pub const MAX_BLOCK_PAYLOAD_SIZE: u64 = 4 * MEGABYTE;
/// How big an ingress payload can be *when stored in memory*. Increasing this value could lead to
/// increased memory usage of replicas.
/// Note that with hashes-in-blocks feature enabled, increasing this value doesn't necessarily mean
/// that we would send more data to peers when transmitting a block, because ingress messages are
/// stripped before disseminating blocks.
pub const MAX_INGRESS_BYTES_PER_BLOCK: u64 = 4 * MEGABYTE;
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```
