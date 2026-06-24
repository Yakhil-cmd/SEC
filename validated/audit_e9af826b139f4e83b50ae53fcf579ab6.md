All five code claims check out against the actual source. Let me confirm the final critical detail about deduplication:Audit Report

## Title
Unbounded Unvalidated DKG Pool Growth via Deferred Remote Dealings with Unknown Target IDs — (File: `rs/consensus/dkg/src/lib.rs`)

## Summary
A Byzantine subnet member can inject an unbounded number of DKG dealing messages with fabricated `NiDkgTargetSubnet::Remote(unknown_id)` values into the unvalidated pool. Because `validate_dealings_for_dealer` silently defers these messages before any signature or dealer-identity check, and because neither the DKG pool nor the P2P slot table imposes any size cap, these messages accumulate in memory for an entire DKG interval (~60 blocks), enabling memory exhaustion and consensus participation degradation.

## Finding Description

**Deferred return for unknown remote target IDs (root cause):**
In `validate_dealings_for_dealer`, when `configs.get(message_dkg_id)` returns `None` and `message_dkg_id.target_subnet.is_remote()` is true, the function returns `Mutations::new()` immediately — before any dealer identity or signature verification. [1](#0-0) 

This means any peer that can advertise a P2P artifact can inject deferred messages; the attacker's node key is never checked at this code path.

**No deduplication across distinct target IDs:**
`DkgMessageId` is derived from `crypto_hash(msg)`, which covers the full message including `dkg_id.target_subnet`. [2](#0-1) 

Each unique `NiDkgTargetId` ([i as u8; 32]) produces a distinct hash and a distinct map entry in the unvalidated pool — no deduplication occurs.

**`on_state_change` groups by `(signer, dkg_id)` and defers each group:**
Each injected message with a distinct `dkg_id.target_subnet` forms its own group, so `validate_dealings_for_dealer` is called once per injected message and returns `Mutations::new()` for each. [3](#0-2) 

**`PoolSection` has no quota or eviction:**
`DkgPoolImpl` stores unvalidated artifacts in a plain `BTreeMap`-backed `PoolSection` with no size cap. [4](#0-3) [5](#0-4) 

**P2P slot table uses `SLOT_TABLE_NO_LIMIT` for DKG:**
The DKG broadcast channel is constructed with `usize::MAX` as the per-peer slot cap, so there is no bound on how many DKG slot advertisements a single peer can make. [6](#0-5) [7](#0-6) 

Compare to ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`. [8](#0-7) 

**Purge only at DKG interval boundaries:**
The pool is only purged when `start_height > dkg_pool.get_current_start_height()`, i.e., at the start of a new DKG interval. Deferred messages accumulate for the entire interval. [9](#0-8) 

**`DkgBouncer` accepts all messages at the current start height without checking `target_subnet`:** [10](#0-9) 

## Impact Explanation
A Byzantine subnet member can exhaust replica memory by flooding the unvalidated DKG pool with dealings referencing fabricated remote target IDs. NiDKG dealings contain non-trivial cryptographic material; with no slot limit and no pool size cap, thousands of distinct artifacts can be inserted per interval. Sustained across intervals, this can crash replicas or degrade consensus participation to the point of halting subnet progress. This matches the allowed impact: **High — Application/platform-level DoS, consensus blocking, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation
The attacker must be a legitimate subnet member (a Byzantine node within the fault threshold). This is the standard Byzantine adversary model for IC subnet protocols — no external access, leaked keys, governance majority, or host compromise is required. The attack is repeatable across every DKG interval and requires only the ability to advertise P2P artifacts with the attacker's own node identity. The exploit path is straightforward and deterministic.

## Recommendation
1. **Reject unknown remote target IDs immediately** when `start_block_height` matches the current interval but no config exists for the `dkg_id`. The deferral window should be bounded, e.g., by checking whether the `NiDkgTargetId` appears in any pending `SetupInitialDKG` or `ReshareChainKey` context in certified state before deferring; if not, issue `HandleInvalid`.
2. **Apply a per-peer slot table limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS = 50_000` in `rs/replica/setup_ic_network/src/lib.rs`.
3. **Add a count-based or byte-based cap** inside `DkgPoolImpl::insert()` (or `PoolSection`) to reject insertions beyond a configurable threshold, consistent with how `IngressPoolImpl` enforces `ingress_pool_max_count` and `ingress_pool_max_bytes`.

## Proof of Concept
```
1. Byzantine node B is a legitimate member of subnet S.
2. B crafts N DKG dealing messages, each with:
   - content.dkg_id.start_block_height = current DKG interval start height
   - content.dkg_id.target_subnet = NiDkgTargetSubnet::Remote([i as u8; 32]) for i in 0..N
   - signature.signer = B's own NodeId
3. B advertises each message on a distinct P2P slot number.
4. Receiving replicas: DkgBouncer returns BouncerValue::Wants (height matches),
   SLOT_TABLE_NO_LIMIT allows all slot advertisements.
5. Each message is fetched and inserted into the unvalidated pool via DkgPoolImpl::insert().
6. on_state_change groups by (signer, dkg_id); each group has a unique dkg_id,
   so validate_dealings_for_dealer is called N times, returning Mutations::new() each time.
7. Pool grows by N entries, each holding a full NiDKG dealing, until the next interval purge.
8. Repeat across intervals to sustain memory pressure.

Verification: A unit test can confirm this by inserting N messages with distinct
NiDkgTargetSubnet::Remote([i; 32]) values into a DkgPoolImpl, calling on_state_change
with an empty configs map, and asserting that get_unvalidated().count() == N after
each call (no removals occur).
```

### Citations

**File:** rs/consensus/dkg/src/lib.rs (L207-211)
```rust
        let config = match configs.get(message_dkg_id) {
            Some(config) => config,
            None if message_dkg_id.target_subnet.is_remote() => {
                return Mutations::new();
            }
```

**File:** rs/consensus/dkg/src/lib.rs (L302-304)
```rust
        if start_height > dkg_pool.get_current_start_height() {
            return ChangeAction::Purge(start_height).into();
        }
```

**File:** rs/consensus/dkg/src/lib.rs (L340-362)
```rust
        let dealings: Vec<Vec<&Message>> = dkg_pool
            .get_unvalidated()
            // Group all unvalidated dealings by (dealer, DKG ID).
            .fold(BTreeMap::new(), |mut map, dealing| {
                let key = (dealing.signature.signer, dealing.content.dkg_id.clone());
                let dealings: &mut Vec<_> = map.entry(key).or_default();
                dealings.push(dealing);
                processed += 1;
                map
            })
            // Get the dealings sorted by (dealer, DKG ID)
            .into_values()
            .collect();

        let changeset = dealings
            .par_iter()
            .map(|dealings| {
                self.validate_dealings_for_dealer(dkg_pool, &configs, start_height, dealings)
            })
            .collect::<Vec<Mutations>>()
            .into_iter()
            .flatten()
            .collect::<Mutations>();
```

**File:** rs/consensus/dkg/src/lib.rs (L391-404)
```rust
impl<Pool: DkgPool> BouncerFactory<DkgMessageId, Pool> for DkgBouncer {
    fn new_bouncer(&self, dkg_pool: &Pool) -> Bouncer<DkgMessageId> {
        let _timer = self.metrics.update_duration.start_timer();

        let start_height = dkg_pool.get_current_start_height();
        Box::new(move |id| {
            use std::cmp::Ordering;
            match id.height.cmp(&start_height) {
                Ordering::Equal => BouncerValue::Wants,
                Ordering::Greater => BouncerValue::MaybeWantsLater,
                Ordering::Less => BouncerValue::Unwanted,
            }
        })
    }
```

**File:** rs/types/types/src/consensus/dkg.rs (L48-54)
```rust
impl From<&Message> for DkgMessageId {
    fn from(msg: &Message) -> Self {
        Self {
            hash: crypto_hash(msg),
            height: msg.content.dkg_id.start_block_height,
        }
    }
```

**File:** rs/artifact_pool/src/pool_common.rs (L19-41)
```rust
pub(crate) struct PoolSection<K, V> {
    messages: BTreeMap<K, V>,
    metrics: PoolMetrics,
}

impl<K: Ord, V: HasLabel> PoolSection<K, V> {
    pub(crate) fn new(metrics_registry: MetricsRegistry, pool: &str, pool_type: &str) -> Self {
        Self {
            messages: Default::default(),
            metrics: PoolMetrics::new(metrics_registry, pool, pool_type),
        }
    }

    pub(crate) fn insert(&mut self, key: K, value: V) -> Option<V> {
        self.metrics
            .observe_insert(MESSAGE_SIZE_BYTES, value.label());
        let replaced = self.messages.insert(key, value);
        if let Some(replaced) = &replaced {
            self.metrics
                .observe_duplicate(MESSAGE_SIZE_BYTES, replaced.label());
        }
        replaced
    }
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L88-92)
```rust
    /// Inserts an unvalidated artifact into the unvalidated section.
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L271-280)
```rust
        let dkg = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.dkg_pool.clone(),
                bouncers.dkg,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };
```
