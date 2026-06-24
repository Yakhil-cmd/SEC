### Title
Ingress Message Ordering Manipulation via Proposer-Targeted Timestamp Race — (`rs/ingress_manager/src/ingress_selector.rs`)

---

### Summary

The ingress selector sorts messages within a canister's queue by the local node's receive timestamp (`artifact.timestamp`). Because the rank-0 block maker for any upcoming height is deterministically computable from the publicly gossiped random beacon, an attacker can submit their ingress message directly to the next proposer node and obtain an earlier receive timestamp than a competing message that arrives via P2P gossip. This gives the attacker priority in block inclusion order for the same canister — the IC analog of the Morpho DLL-head frontrunning attack.

---

### Finding Description

**Step 1 — Ordering is by local receive timestamp, not consensus time.**

In `get_ingress_payload`, after fetching all validated artifacts for the current expiry window, the code explicitly re-sorts each canister's message queue by pool arrival time to defeat expiry-time manipulation:

```rust
// At this point messages are sorted by expiry time. In order to prevent malicious
// users from putting their messages ahead of others by carefully crafting the expiry
// times, we sort the ingress messages by the time they were delivered to the pool.
// NOTE: We sort in reverse order, because messages are pop()-ed from the back.
for v in canister_queues.values_mut() {
    v.msgs.sort_unstable_by_key(|artifact| {
        std::cmp::Reverse(artifact.timestamp.as_nanos_since_unix_epoch())
    });
}
``` [1](#0-0) 

Because the sort is `Reverse(timestamp)` and messages are `pop()`-ed from the back, the message with the **smallest (earliest) timestamp is processed first** — FIFO by arrival time.

**Step 2 — The timestamp is the local node's wall clock, not a consensus-agreed value.**

The timestamp is stamped in `IngressProcessor::process_changes` at the moment the artifact manager receives the message:

```rust
let unvalidated_artifact = UnvalidatedArtifact {
    message,
    peer_id,
    timestamp: time_source.get_relative_time(),   // local node clock
};
ingress_pool.insert(unvalidated_artifact);
``` [2](#0-1) 

This timestamp is preserved verbatim when the artifact is promoted to the validated pool:

```rust
self.validated.insert(
    message_id,
    ValidatedIngressArtifact {
        msg: unvalidated_artifact.message,
        timestamp: unvalidated_artifact.timestamp,   // carried over unchanged
    },
);
``` [3](#0-2) 

Different nodes assign different timestamps to the same message. The block proposer uses **its own** timestamps when calling `get_ingress_payload`.

**Step 3 — The rank-0 block maker is publicly predictable from the random beacon.**

Block maker ranking is computed deterministically from the previous random beacon:

```rust
pub fn get_block_maker_rank(
    &self,
    height: Height,
    previous_beacon: &RandomBeacon,
    node_id: NodeId,
) -> Result<Option<Rank>, MembershipError> {
    let shuffled_nodes = self.get_shuffled_nodes(
        height,
        previous_beacon,
        &RandomnessPurpose::BlockmakerRanking,
    )?;
    ...
}
``` [4](#0-3) 

The shuffle uses `Csprng::from_random_beacon_and_purpose(previous_beacon, &BlockmakerRanking)`: [5](#0-4) 

The random beacon for height `h` is gossiped to all subnet nodes and is publicly readable. Any observer can therefore compute the rank-0 block maker for height `h+1` before that block is proposed.

**Step 4 — The attack window.**

A message submitted directly to the proposer node's HTTP endpoint is timestamped when it enters the artifact manager on that node. A message submitted to any other node is gossiped to the proposer via P2P, adding network latency (typically 100–500 ms on mainnet). The attacker's directly-submitted message therefore receives an earlier timestamp and is placed at the front of the canister's queue.

---

### Impact Explanation

An unprivileged ingress sender can reliably ensure their message for canister C is executed **before** a victim's message for the same canister in the same block. Concretely:

- **DEX / AMM canisters**: frontrun a swap order to extract value.
- **Governance canisters**: ensure a vote or proposal action lands before a competing one.
- **Any state-sensitive canister**: observe a pending state-changing call and race to execute a conflicting call first.

The attacker does not need to "withdraw" excess resources (unlike the Morpho sandwich); the attack is simpler — a pure ordering manipulation. The victim's message is still included in the same block (or a later one), but the attacker's message executes first, potentially changing the state the victim's message observes.

---

### Likelihood Explanation

**Medium.** The prerequisites are:

1. **Predict the proposer** — trivially computable from the public random beacon using the same `get_shuffled_nodes` logic any replica runs. No privileged access required.
2. **Submit to the proposer's HTTP endpoint** — all replica nodes expose a public HTTPS endpoint. The attacker submits their message directly to the identified proposer node.
3. **Race the gossip latency** — the attacker's message must arrive at the proposer before the victim's gossip-propagated message. Gossip latency is non-zero and predictable; the attacker has a reliable window.

The attack is non-atomic (two separate submissions) but does not require any privileged role, key material, or majority corruption.

---

### Recommendation

1. **Use consensus time for intra-canister ordering.** Replace `artifact.timestamp` (local receive time) with the block's `ValidationContext.time` (a consensus-agreed value) as the ordering key. All honest nodes agree on this value, so it cannot be manipulated by targeting a specific proposer.

2. **Alternatively, use a per-block VRF tiebreaker.** Derive a per-block random permutation from the block's randomness (already available via `RandomnessPurpose::ExecutionThread`) and apply it to break ties within a canister's queue, making the final order unpredictable to the attacker at submission time.

3. **Document the residual risk.** If neither mitigation is adopted, document that intra-canister message ordering within a block is subject to proposer-targeted timing attacks, analogous to the acknowledged Morpho DLL-head issue.

---

### Proof of Concept

```
Height h random beacon B_h is gossiped to all nodes (publicly readable).

Attacker computes:
  shuffled = get_shuffled_nodes(h+1, B_h, BlockmakerRanking)
  proposer = shuffled[0]   // rank-0 node ID → known public endpoint

Victim submits M_victim to node Y (not the proposer).
  → M_victim propagates via P2P gossip to proposer with latency Δ.

Attacker submits M_attacker to proposer's HTTP endpoint directly.
  → M_attacker is timestamped T_attacker at the proposer.
  → M_victim arrives at the proposer at T_attacker + Δ, timestamped T_victim = T_attacker + Δ.

In get_ingress_payload (rs/ingress_manager/src/ingress_selector.rs:151-153):
  canister_queue.msgs sorted by Reverse(timestamp):
    [M_victim (T_victim), M_attacker (T_attacker)]   // newest first
  pop() removes from back → M_attacker (T_attacker < T_victim) is popped first.

Result: M_attacker is included before M_victim in the block payload.
        M_attacker executes first; M_victim observes the post-attacker state.
``` [1](#0-0) [2](#0-1) [6](#0-5) [3](#0-2)

### Citations

**File:** rs/ingress_manager/src/ingress_selector.rs (L146-154)
```rust
        // At this point messages are sorted by expiry time. In order to prevent malicious
        // users from putting their messages ahead of others by carefully crafting the expiry
        // times, we sort the ingress messages by the time they were delivered to the pool.
        // NOTE: We sort in reverse order, because messages are pop()-ed from the back.
        for v in canister_queues.values_mut() {
            v.msgs.sort_unstable_by_key(|artifact| {
                std::cmp::Reverse(artifact.timestamp.as_nanos_since_unix_epoch())
            });
        }
```

**File:** rs/p2p/artifact_manager/src/lib.rs (L382-388)
```rust
                    UnvalidatedArtifactMutation::Insert((message, peer_id)) => {
                        let unvalidated_artifact = UnvalidatedArtifact {
                            message,
                            peer_id,
                            timestamp: time_source.get_relative_time(),
                        };
                        ingress_pool.insert(unvalidated_artifact);
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L288-294)
```rust
                            self.validated.insert(
                                message_id,
                                ValidatedIngressArtifact {
                                    msg: unvalidated_artifact.message,
                                    timestamp: unvalidated_artifact.timestamp,
                                },
                            );
```

**File:** rs/consensus/utils/src/membership.rs (L70-86)
```rust
    pub fn get_shuffled_nodes(
        &self,
        height: Height,
        previous_beacon: &RandomBeacon,
        purpose: &RandomnessPurpose,
    ) -> Result<Vec<NodeId>, MembershipError> {
        assert_eq!(height, previous_beacon.height().increment());
        let mut node_ids = self.get_nodes(height)?;
        // To achieve a deterministic shuffling, we sort the ids first, to not rely on
        // any ordering by the registry. We assume all node_ids are unique, so
        // `sort_unstable` is effectively the same as `sort` but slightly more
        // efficient.
        node_ids.sort_unstable();
        let mut rng = Csprng::from_random_beacon_and_purpose(previous_beacon, purpose);
        node_ids.shuffle(&mut rng);
        Ok(node_ids)
    }
```

**File:** rs/consensus/utils/src/membership.rs (L91-103)
```rust
    pub fn get_block_maker_rank(
        &self,
        height: Height,
        previous_beacon: &RandomBeacon,
        node_id: NodeId,
    ) -> Result<Option<Rank>, MembershipError> {
        let shuffled_nodes = self.get_shuffled_nodes(
            height,
            previous_beacon,
            &RandomnessPurpose::BlockmakerRanking,
        )?;
        Membership::get_block_maker_rank_from_shuffled_nodes(&node_id, &shuffled_nodes)
    }
```

**File:** rs/crypto/prng/src/lib.rs (L34-47)
```rust
    pub fn from_random_beacon_and_purpose(
        random_beacon: &RandomBeacon,
        purpose: &RandomnessPurpose,
    ) -> Self {
        let randomness = randomness_from_crypto_hashable(random_beacon);
        Self::from_randomness_and_purpose(&randomness, purpose)
    }

    /// Creates a CSPRNG from the Randomness value for the given purpose.
    pub fn from_randomness_and_purpose(seed: &Randomness, purpose: &RandomnessPurpose) -> Self {
        let seed = Seed::from_bytes(&seed.get());
        let seed_for_purpose = seed.derive(&purpose.domain_separator());
        Csprng::from_seed(seed_for_purpose)
    }
```
