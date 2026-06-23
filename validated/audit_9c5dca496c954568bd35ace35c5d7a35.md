### Title
Ticket Flooding Exhausts Shared Stable Memory, Permanently Blocking SNS Swap Canister Upgrades — (`rs/sns/swap/src/memory.rs`, `rs/sns/swap/canister/canister.rs`)

---

### Summary

An unprivileged attacker can create enough open tickets via `new_sale_ticket` to exhaust the 8 GiB stable memory shared between `OPEN_TICKETS_MEMORY` and `UPGRADES_MEMORY`. Once stable memory is full, `canister_pre_upgrade` panics when `Writer` cannot grow `UPGRADES_MEMORY`, permanently blocking all future upgrades. The purge mechanism provides no protection because its threshold (100 M tickets ≈ 25 GB) is set above the physical stable memory capacity (8 GiB).

---

### Finding Description

**Shared memory pool.** All three virtual memories are managed by a single `MemoryManager<DefaultMemoryImpl>`: [1](#0-0) 

`UPGRADES_MEMORY` (id 0) and `OPEN_TICKETS_MEMORY` (id 1) draw pages from the same underlying 8 GiB pool. There is no per-virtual-memory reservation or cap.

**Ticket insertion is unbounded by ICP commitment.** `new_sale_ticket` calls `compute_participation_increment` with `current_direct_participation_e8s()`, which reflects only *actual ICP committed by buyers*, not pending ticket amounts: [2](#0-1) 

An attacker who never transfers ICP keeps `tot_direct_participation` at 0, so the capacity check never fires. Each distinct non-anonymous principal can insert one ticket per call.

**Purge threshold exceeds stable memory capacity.** The periodic purge only activates when ticket count exceeds 100 M: [3](#0-2) 

The inline comment acknowledges `100M * ~size(ticket) = ~25GB`. Since IC canisters are capped at 8 GiB of stable memory, the BTree can hold roughly 30–40 M tickets before OOM. The purge threshold is ~3× the physical capacity, so it **never fires** before stable memory is exhausted.

**Upgrade panics on write failure.** `canister_pre_upgrade` uses `.expect()` on every write to `UPGRADES_MEMORY`: [4](#0-3) 

`Writer::new(&mut um, 0)` on a `VirtualMemory` backed by a full `DefaultMemoryImpl` will return `Err` on the first grow attempt. The `.expect()` traps, the upgrade rolls back, and the canister remains on the old wasm — indefinitely.

---

### Impact Explanation

- The SNS swap canister becomes permanently unupgradeable for the duration of the attack.
- The NNS cannot push security patches, parameter changes, or lifecycle fixes to the affected swap.
- The canister continues to accept ingress (swap operations still work), but any governance-triggered upgrade will keep failing.
- There is no automatic recovery path: the purge never triggers (ticket count < 100 M threshold), and the only way to free memory is to purge tickets — which requires an upgrade that is itself blocked.

---

### Likelihood Explanation

- **Principals needed**: ~30–40 M distinct non-anonymous IC principals to fill 8 GiB.
- **Cost**: Each `new_sale_ticket` is an update call. At ~590 K cycles/call, 35 M calls ≈ 20 T cycles ≈ $20–$100 USD at current rates. Low cost for a motivated attacker targeting a high-value SNS launch.
- **Time**: IC ingress throughput for a single canister is on the order of hundreds of messages/second. At 500 msg/s, 35 M tickets takes ~70,000 seconds (~19 hours). SNS swaps last 1–14 days, so the window is sufficient.
- **No privilege required**: `new_sale_ticket` is a public update endpoint, callable by any non-anonymous principal during `Lifecycle::Open`.

---

### Recommendation

1. **Add a hard cap on ticket count** inside `new_sale_ticket` — e.g., reject if `OPEN_TICKETS_MEMORY.len() >= MAX_TICKETS` where `MAX_TICKETS` is derived from a safe fraction of available stable memory (e.g., 5 M).
2. **Lower `NUMBER_OF_TICKETS_THRESHOLD`** to a value well below the stable memory capacity, or replace it with a memory-usage-based trigger.
3. **Pre-grow `UPGRADES_MEMORY`** at canister init/post-upgrade to reserve a guaranteed minimum number of pages before `OPEN_TICKETS_MEMORY` can claim them.
4. **Use `write` without `.expect()`** in `canister_pre_upgrade` and instead log and gracefully handle OOM, or pre-check available memory before serializing.

---

### Proof of Concept

```
1. Deploy SNS swap in Lifecycle::Open with min_participant_icp_e8s = 1_000_000 (0.01 ICP).
2. Generate 35_000_000 distinct PrincipalIds (p_0 … p_N).
3. For each p_i, call new_sale_ticket({ amount_icp_e8s: 1_000_000 }).
   - No ICP transfer needed; tickets accumulate in OPEN_TICKETS_MEMORY.
4. Observe stable_memory_num_pages metric approaching 131_072 (8 GiB / 64 KiB).
5. Trigger upgrade_canister via NNS proposal.
6. canister_pre_upgrade fires:
   - Writer::new(&mut um, 0) on UPGRADES_MEMORY (0 pages allocated).
   - writer.write(...) attempts memory.grow(1) on the full DefaultMemoryImpl → returns -1.
   - .expect("Error. Couldn't write to stable memory") panics.
7. Upgrade fails; canister remains on old wasm.
8. Repeat step 5 indefinitely — upgrade always fails.
9. try_purge_old_tickets never fires (ticket count ~35 M < 100 M threshold).
``` [5](#0-4) [6](#0-5) [7](#0-6) [2](#0-1) [8](#0-7)

### Citations

**File:** rs/sns/swap/src/memory.rs (L11-27)
```rust
const UPGRADES_MEMORY_ID: MemoryId = MemoryId::new(0);
const OPEN_TICKETS_MEMORY_ID: MemoryId = MemoryId::new(1);
const BUYERS_INDEX_LIST_MEMORY_ID: MemoryId = MemoryId::new(2);

thread_local! {

    static MEMORY_MANAGER: RefCell<MemoryManager<DefaultMemoryImpl>> = RefCell::new(
        MemoryManager::init(DefaultMemoryImpl::default())
    );

    // The memory where the swap canister must write and read its state during an upgrade.
    pub static UPGRADES_MEMORY: RefCell<VirtualMemory<DefaultMemoryImpl>> = MEMORY_MANAGER.with(|memory_manager|
        RefCell::new(memory_manager.borrow().get(UPGRADES_MEMORY_ID)));

    // The stable bmap where the swap canister keeps open tickets. The key is the Principal.
    pub static OPEN_TICKETS_MEMORY: RefCell<StableBTreeMap<Blob<{PrincipalId::MAX_LENGTH_IN_BYTES}>, Ticket, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager| RefCell::new(StableBTreeMap::init(memory_manager.borrow().get(OPEN_TICKETS_MEMORY_ID))));
```

**File:** rs/sns/swap/src/swap.rs (L1018-1027)
```rust
        const NUMBER_OF_TICKETS_THRESHOLD: u64 = 100_000_000; // 100M * ~size(ticket) = ~25GB
        const TWO_DAYS_IN_NANOSECONDS: u64 = 60 * 60 * 24 * 2 * 1_000_000_000;
        const MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT: u64 = 100_000;

        self.try_purge_old_tickets(
            ic_cdk::api::time,
            NUMBER_OF_TICKETS_THRESHOLD,
            TWO_DAYS_IN_NANOSECONDS,
            MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT,
        );
```

**File:** rs/sns/swap/src/swap.rs (L2563-2594)
```rust
        let amount_icp_e8s = match compute_participation_increment(
            self.current_direct_participation_e8s(),
            params.max_direct_participation_icp_e8s.expect(
                "`params.max_direct_participation_icp_e8s` should always be set during Swap's initialization",
            ),
            params.min_participant_icp_e8s,
            params.max_participant_icp_e8s,
            old_balance_e8s,
            request.amount_icp_e8s,
        ) {
            Ok(amount_icp_e8s) => amount_icp_e8s,
            Err((min, max)) => return NewSaleTicketResponse::err_invalid_user_amount(min, max),
        };

        let account = Some(Icrc1Account {
            owner: Some(caller),
            subaccount: request.subaccount.clone(),
        });

        let ticket_id = self.next_ticket_id.unwrap_or(0);
        self.next_ticket_id = Some(ticket_id.saturating_add(1));
        // the amount_icp_e8s is the actual_increment_e8s of the user and not necessarily was the user put in the ticket.
        // This can potentially reduce the amount of tokens to transfer/refund
        let ticket = Ticket {
            ticket_id,
            account,
            amount_icp_e8s,
            creation_time: time,
        };
        memory::OPEN_TICKETS_MEMORY.with(|m| {
            m.borrow_mut().insert(principal, ticket.clone());
        });
```

**File:** rs/sns/swap/src/swap.rs (L3231-3233)
```rust
    if tot_direct_participation >= max_tot_direct_participation {
        return Err((0, 0));
    }
```

**File:** rs/sns/swap/canister/canister.rs (L386-407)
```rust
fn canister_pre_upgrade() {
    log!(INFO, "Executing pre upgrade");

    // serialize the state
    let mut state_bytes = vec![];
    swap()
        .encode(&mut state_bytes)
        .expect("Error. Couldn't serialize canister pre-upgrade.");

    // Write the length of the serialized bytes to memory, followed by the
    // by the bytes themselves.
    UPGRADES_MEMORY.with(|um| {
        let mut um = um.borrow_mut().to_owned();
        let mut writer = Writer::new(&mut um, 0);
        writer
            .write(&(state_bytes.len() as u32).to_le_bytes())
            .expect("Error. Couldn't write to stable memory");
        writer
            .write(&state_bytes)
            .expect("Error. Couldn't write to stable memory");
    })
}
```
