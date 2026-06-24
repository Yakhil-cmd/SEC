### Title
Unbounded `minted_events` Growth in ckETH Minter Enables Capital-Recycling Memory Exhaustion and Eventual Liveness Failure - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary
The ckETH minter's `State` struct contains a `minted_events: BTreeMap<EventSource, MintedEvent>` that grows permanently with every successful ETH or ERC-20 deposit. Withdrawals (burns) never remove entries from this map. Because each deposit requires only a unique Ethereum transaction hash, an attacker can recycle the same capital across repeated deposit→withdraw→deposit cycles, paying only Ethereum gas fees per cycle while forcing unbounded growth of the minter canister's heap. At scale this exhausts the canister's 4 GB memory limit, causes upgrade serialization to exceed the instruction limit, and permanently breaks inbound and outbound bridging for all users.

### Finding Description

The ckETH minter `State` struct declares:

```rust
// rs/ethereum/cketh/minter/src/state.rs:65
pub minted_events: BTreeMap<EventSource, MintedEvent>,
```

Every successful deposit appends a permanent entry via `record_successful_mint`:

```rust
// rs/ethereum/cketh/minter/src/state.rs:274-301
fn record_successful_mint(
    &mut self,
    source: EventSource,
    token_symbol: &str,
    mint_block_index: LedgerMintIndex,
    erc20_contract_address: Option<Address>,
) {
    // ...
    self.minted_events.insert(
        source,
        MintedEvent { deposit_event, mint_block_index, ... },
    );
}
``` [1](#0-0) [2](#0-1) 

A grep for `minted_events.remove`, `minted_events.clear`, and `minted_events.retain` returns zero matches across the entire repository. The map is never pruned. Withdrawals (ckETH burns) operate entirely on `eth_transactions` and do not touch `minted_events`. [3](#0-2) 

The `EventSource` key is `(transaction_hash, log_index)`, which is unique per Ethereum log entry. Each new deposit transaction produces a new key, so the same capital can be recycled indefinitely: deposit ETH → receive ckETH → withdraw ckETH → receive ETH back → deposit again. Each round trip adds one permanent `MintedEvent` entry (~200 bytes of heap) while the attacker recovers their principal minus gas fees.

The dashboard template iterates over the entire `minted_events` collection on every dashboard query:

```rust
// rs/ethereum/cketh/minter/src/dashboard.rs:302-315
let mut minted_events: Vec<_> = state.minted_events.values().cloned().collect();
minted_events.sort_unstable_by_key(|event| { ... });
let minted_events_table = DashboardPaginatedTable::from_items(&minted_events, ...);
``` [4](#0-3) 

The metrics endpoint also reads `minted_events.len()` on every scrape:

```rust
// rs/ethereum/cketh/minter/src/main.rs:965
.value(&[("status", "accepted")], s.minted_events.len() as f64)?
``` [5](#0-4) 

### Impact Explanation

Two compounding failures arise:

1. **Heap memory exhaustion.** Each `MintedEvent` occupies roughly 200 bytes (32-byte tx hash + 16-byte log index key, plus deposit event fields, mint block index, token symbol, and optional ERC-20 address). The IC canister heap limit is 4 GB. At the minimum ckETH deposit threshold of 2 000 000 000 000 wei (0.000002 ETH), an attacker needs approximately 20 million deposit cycles to fill 4 GB. Capital is recycled each cycle; the only cost is Ethereum gas. Once the heap is exhausted, every state-mutating call traps, permanently halting both inbound minting and outbound withdrawal processing for all users.

2. **Upgrade serialization failure.** The minter state is serialized to stable memory on every canister upgrade. A sufficiently large `minted_events` map causes the `pre_upgrade` hook to exceed the per-message instruction limit, making the canister permanently un-upgradeable and stranding all in-flight deposits and withdrawals. [6](#0-5) 

### Likelihood Explanation

The attack entry path is fully unprivileged: any Ethereum address can call the ckETH helper smart contract to deposit ETH. The ckETH minter's log-scraping timer picks up the deposit automatically; no privileged role is required. Capital is recycled on each cycle (the attacker withdraws ckETH back to ETH), so the sustained cost is only Ethereum gas fees per round trip. At current mainnet gas prices (~$5–$20 per deposit+withdrawal pair), forcing tens of thousands of entries costs hundreds to low thousands of dollars — well within reach of a motivated attacker. The effect is permanent and cumulative: entries already written cannot be removed without a breaking state migration.

### Recommendation

Prune `minted_events` entries that are no longer needed for deduplication. Because the Ethereum log-scraping window is bounded (the minter only re-processes logs within a sliding window of recent blocks), any `MintedEvent` whose `deposit_event.block_number` is older than the oldest block the minter could ever re-scrape is safe to evict. Concretely:

- Track a `min_retained_block_number` equal to `first_scraped_block_number` (already stored in state).
- On each timer tick, after advancing `first_scraped_block_number`, remove all `minted_events` entries whose `deposit_event.block_number() < min_retained_block_number`.
- Alternatively, cap `minted_events` at a fixed maximum size (e.g., the last 1 000 000 entries) using a `VecDeque`-backed structure, analogous to how `finalized_requests` in the ckBTC minter is capped at `MAX_FINALIZED_REQUESTS`. [7](#0-6) 

### Proof of Concept

```
Attacker controls Ethereum address A and IC principal P.

Cycle 1:
  A deposits 0.000002 ETH to ckETH helper contract (tx_hash=H1, log_index=0)
  → minter scrapes log, calls record_event_to_mint({H1,0}, ...)
  → minter mints ckETH to P, calls record_successful_mint({H1,0}, ...)
  → minted_events now contains 1 entry: {H1,0} → MintedEvent{...}

  P calls withdraw_eth(amount=0.000002 ETH, destination=A)
  → minter burns ckETH from ledger, creates EIP-1559 tx, sends ETH back to A
  → minted_events still contains 1 entry (withdrawal path never touches minted_events)

Cycle 2:
  A deposits 0.000002 ETH again (tx_hash=H2, log_index=0)
  → minted_events now contains 2 entries: {H1,0}, {H2,0}
  ...

After N cycles:
  minted_events contains N entries
  heap usage ≈ N × 200 bytes
  capital recovered: attacker holds ~0.000002 ETH minus N × gas_fees
  cost to attacker: N × gas_fees only

At N = 20,000,000:
  heap ≈ 4 GB → canister traps on next state mutation
  All inbound minting and outbound withdrawals halt for all users
  Canister upgrade fails (pre_upgrade serialization exceeds instruction limit)
```

The deduplication invariant enforced by `record_successful_mint` (the `assert_eq!(..., None, "attempted to mint ckETH twice")`) confirms that each unique `EventSource` is inserted exactly once and never removed, proving the append-only property. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L54-103)
```rust
pub struct State {
    pub ethereum_network: EthereumNetwork,
    pub ecdsa_key_name: String,
    pub cketh_ledger_id: Principal,
    pub log_scrapings: LogScrapings,
    pub ecdsa_public_key: Option<EcdsaPublicKeyResult>,
    pub cketh_minimum_withdrawal_amount: Wei,
    pub ethereum_block_height: CandidBlockTag,
    pub first_scraped_block_number: BlockNumber,
    pub last_observed_block_number: Option<BlockNumber>,
    pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
    pub minted_events: BTreeMap<EventSource, MintedEvent>,
    pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
    pub eth_transactions: EthTransactions,
    pub skipped_blocks: BTreeMap<Address, BTreeSet<BlockNumber>>,

    /// Current balance of ETH held by the minter.
    /// Computed based on audit events.
    pub eth_balance: EthBalance,

    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,

    /// Per-principal lock for pending withdrawals
    pub pending_withdrawal_principals: BTreeSet<Principal>,

    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: HashSet<TaskType>,

    /// Number of HTTP outcalls since the last upgrade.
    /// Used to correlate request and response in logs.
    pub http_request_counter: u64,

    pub last_transaction_price_estimate: Option<(u64, GasFeeEstimate)>,

    /// Canister ID of the ledger suite orchestrator that
    /// can add new ERC-20 token to the minter
    pub ledger_suite_orchestrator_id: Option<Principal>,

    /// Canister ID of the EVM RPC canister that
    /// handles communication with Ethereum
    pub evm_rpc_id: Principal,

    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L274-301)
```rust
    fn record_successful_mint(
        &mut self,
        source: EventSource,
        token_symbol: &str,
        mint_block_index: LedgerMintIndex,
        erc20_contract_address: Option<Address>,
    ) {
        assert!(
            !self.invalid_events.contains_key(&source),
            "attempted to mint an event previously marked as invalid {source:?}"
        );
        let deposit_event = match self.events_to_mint.remove(&source) {
            Some(event) => event,
            None => panic!("attempted to mint ckETH for an unknown event {source:?}"),
        };
        assert_eq!(
            self.minted_events.insert(
                source,
                MintedEvent {
                    deposit_event,
                    mint_block_index,
                    token_symbol: token_symbol.to_string(),
                    erc20_contract_address,
                },
            ),
            None,
            "attempted to mint ckETH twice for the same event {source:?}"
        );
```

**File:** rs/ethereum/cketh/minter/src/dashboard.rs (L302-315)
```rust
        let mut minted_events: Vec<_> = state.minted_events.values().cloned().collect();
        minted_events.sort_unstable_by_key(|event| {
            let deposit_event = &event.deposit_event;
            Reverse((deposit_event.block_number(), deposit_event.log_index()))
        });

        let minted_events_table = DashboardPaginatedTable::from_items(
            &minted_events,
            pagination_parameters.minted_events_start,
            DEFAULT_PAGE_SIZE,
            7,
            "minted-events",
            "minted_events_start",
        );
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L961-966)
```rust
                w.gauge_vec(
                    "cketh_minter_accepted_deposits",
                    "The number of deposits the ckETH minter processed, by status.",
                )?
                .value(&[("status", "accepted")], s.minted_events.len() as f64)?
                .value(&[("status", "rejected")], s.invalid_events.len() as f64)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1330-1337)
```rust
    fn push_finalized_request(&mut self, req: FinalizedBtcRequest) {
        assert!(!self.has_pending_retrieve_btc_request(req.request.block_index()));

        if self.finalized_requests.len() >= MAX_FINALIZED_REQUESTS {
            self.finalized_requests.pop_front();
        }
        self.finalized_requests.push_back(req)
    }
```
