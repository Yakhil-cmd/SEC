### Title
Missing Emitter-Address Validation in `interop_root_reporter_event_hook` Allows Arbitrary Interop Root Injection — (`system_hooks/src/event_hooks/interop_root_reporter.rs`)

---

### Summary

The `interop_root_reporter_event_hook` processes `InteropRootAdded` events and writes interop roots directly into system state via `system.io.add_interop_root`. The hook never receives or validates the emitter address of the triggering event. The dispatch layer (`HooksStorage`) keys event hooks by only the **low 32 bits** of the emitter address (`for_address_low: u32`). An unprivileged attacker can use CREATE2 to deploy a contract whose address shares those low 32 bits with `L2_INTEROP_ROOT_STORAGE`, emit a crafted `InteropRootAdded` event, and inject arbitrary interop roots — including zero roots and zero chain IDs — into the system's interop root storage without any on-chain authorization.

---

### Finding Description

**Root cause 1 — 32-bit address truncation in event hook dispatch.**

`HooksStorage` stores event hooks in a `BTreeMap<u32, SystemEventHook<S>>`, keyed by the low 32 bits of the emitter address: [1](#0-0) 

```rust
pub struct HooksStorage<S: SystemTypes, A: Allocator + Clone> {
    call_hooks: BTreeMap<u16, SystemCallHook<S>, A>,
    event_hooks: BTreeMap<u32, SystemEventHook<S>, A>,
}
```

Registration uses `for_address_low: u32`: [2](#0-1) 

`L2_INTEROP_ROOT_STORAGE` lives at `0x1000a` (low 32 bits = `0x0001000a`). Any contract whose address shares those low 32 bits will match the same BTreeMap key and trigger the hook.

**Root cause 2 — Hook function receives no emitter address.**

The hook signature is: [3](#0-2) 

```rust
pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<...>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
```

There is no `emitter: B160` parameter. The hook cannot perform any address check of its own.

**Root cause 3 — No validation of `root` or `chain_id` before state write.**

After parsing, the hook calls `add_interop_root` unconditionally: [4](#0-3) 

```rust
let root = Bytes32::from_array(data[64..96].try_into().unwrap());
let chain_id = U256::from_be_bytes(topics[1].as_u8_array());
let block_or_batch_number = U256::from_be_bytes(topics[2].as_u8_array());
system.io.add_interop_root(
    ExecutionEnvironmentType::NoEE,
    resources,
    InteropRoot { root, block_or_batch_number, chain_id },
)?;
```

The `InteropRoot` struct documents that `root` **cannot be zero** and `chain_id` **must be non-zero**: [5](#0-4) 

Neither the hook nor `push_root` enforces these invariants: [6](#0-5) 

The only enforcement is inside the EVM-level `L2_INTEROP_ROOT_STORAGE` contract, which the hook entirely bypasses when triggered from a spoofed address.

---

### Impact Explanation

An attacker who injects fake interop roots can:

- Corrupt the interop root rolling hash committed at batch finalization, causing forward/proving divergence.
- Inject zero roots or zero chain IDs, violating documented invariants and potentially causing panics or incorrect cross-chain state proofs.
- Overwrite or shadow legitimate interop roots for a target chain ID / block number pair, enabling cross-chain replay or denial of valid interop messages.

This is a **state-transition bug** with a **public funds-loss path** via corrupted cross-chain state.

---

### Likelihood Explanation

The attacker is an unprivileged transaction sender. The only prerequisite is deploying a contract at an address whose low 32 bits equal `0x0001000a`. Using CREATE2, this requires brute-forcing ~2³² keccak256 evaluations on average — approximately 4 seconds on commodity GPU hardware. No privileged access, governance majority, or oracle manipulation is required.

---

### Recommendation

1. **Pass the emitter address into the event hook** and validate it against the canonical `L2_INTEROP_ROOT_STORAGE` address before processing any data.
2. **Key `event_hooks` by the full 160-bit address** (or at minimum a cryptographically sufficient prefix) rather than only 32 bits.
3. **Add explicit invariant checks** inside `interop_root_reporter_event_hook` (and/or `push_root`) that reject zero `root` and zero `chain_id`, independent of the EVM contract's own validation.

---

### Proof of Concept

```
1. Attacker selects deployer address D and init_code C.
2. Attacker iterates salt S until:
       keccak256(0xff ++ D ++ S ++ keccak256(C))[12:] & 0xFFFFFFFF == 0x0001000a
   Expected iterations: ~2^32 ≈ 4 billion (≈4 s on GPU).
3. Attacker deploys C via CREATE2 at the matching address.
4. C emits:
       emit InteropRootAdded(
           chain_id   = 1,          // topics[1]
           block_num  = 999,        // topics[2]
           roots      = [fake_root] // data: offset=32, len=1, root=<arbitrary>
       )
   with topics[0] = INTEROP_ROOT_ADDED_EVENT_SIG.
5. HooksStorage dispatch: emitter_address & 0xFFFFFFFF == 0x0001000a → hook fires.
6. interop_root_reporter_event_hook parses data, skips all address/root/chain_id
   validation, and calls system.io.add_interop_root with attacker-controlled values.
7. Fake interop root is committed into the batch output rolling hash.
``` [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** zk_ee/src/common_structs/system_hooks.rs (L80-126)
```rust
pub struct HooksStorage<S: SystemTypes, A: Allocator + Clone> {
    call_hooks: BTreeMap<u16, SystemCallHook<S>, A>,
    event_hooks: BTreeMap<u32, SystemEventHook<S>, A>,
}

impl<S: SystemTypes, A: Allocator + Clone> HooksStorage<S, A> {
    ///
    /// Creates empty hooks storage with a given allocator.
    ///
    pub fn new_in(allocator: A) -> Self {
        Self {
            call_hooks: BTreeMap::new_in(allocator.clone()),
            event_hooks: BTreeMap::new_in(allocator),
        }
    }

    ///
    /// Adds a new call hook into a given address.
    /// Fails if there was another hook registered there before.
    ///
    pub fn add_call_hook(
        &mut self,
        for_address_low: u16,
        hook: SystemCallHook<S>,
    ) -> Result<(), InternalError> {
        let existing = self.call_hooks.insert(for_address_low, hook);
        if existing.is_some() {
            return Err(internal_error!("System call hook already registered"));
        }
        Ok(())
    }

    ///
    /// Adds a new event hook into a given address.
    /// Fails if there was another hook registered there before.
    ///
    pub fn add_event_hook(
        &mut self,
        for_address_low: u32,
        hook: SystemEventHook<S>,
    ) -> Result<(), InternalError> {
        let existing = self.event_hooks.insert(for_address_low, hook);
        if existing.is_some() {
            return Err(internal_error!("System event hook already registered"));
        }
        Ok(())
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L19-82)
```rust
pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    // First, ensure we're capturing the InteropRootAdded event
    if topics.is_empty() || topics[0].as_u8_array() != INTEROP_ROOT_ADDED_EVENT_SIG {
        return Ok(());
    }
    // Internal error if the data supplied doesn't match the expected value
    if data.len() != 96 {
        return Err(internal_error!("Interop root reporter event hook received bad data").into());
    }

    // Parse data
    let offset: u32 = match U256::from_be_slice(&data[..32]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad offset").into(),
            );
        }
    };
    // This event is part of the system, but we check it anyways
    if offset != 32 {
        return Err(internal_error!("Interop root reporter event hook received bad offset").into());
    }

    let len: u32 = match U256::from_be_slice(&data[32..64]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad length").into(),
            );
        }
    };
    // It should have exactly one side
    if len != 1 {
        return Err(internal_error!("Interop root reporter event hook received bad length").into());
    }
    // Validate topics length
    if topics.len() != 3 {
        return Err(internal_error!("Interop root reporter event hook received bad topics").into());
    }

    let root = Bytes32::from_array(data[64..96].try_into().unwrap());
    let chain_id = U256::from_be_bytes(topics[1].as_u8_array());
    let block_or_batch_number = U256::from_be_bytes(topics[2].as_u8_array());
    system.io.add_interop_root(
        ExecutionEnvironmentType::NoEE,
        resources,
        InteropRoot {
            root,
            block_or_batch_number,
            chain_id,
        },
    )?;

    Ok(())
}
```

**File:** zk_ee/src/common_structs/interop_root_storage.rs (L14-45)
```rust
pub struct InteropRoot {
    /// The merkle root hash (cannot be zero for valid roots)
    pub root: Bytes32,
    /// Block or batch number from the source chain
    pub block_or_batch_number: U256,
    /// Source chain identifier (must be non-zero)
    pub chain_id: U256,
}

pub struct InteropRootStorage<SF: StackFactory<M>, const M: usize, A: Allocator + Clone = Global> {
    list: HistoryList<InteropRoot, (), SF, M, A>,
    _marker: core::marker::PhantomData<A>,
}

impl<SF: StackFactory<M>, const M: usize, A: Allocator + Clone> InteropRootStorage<SF, M, A> {
    pub fn new_from_parts(allocator: A) -> Self {
        Self {
            list: HistoryList::new(allocator),
            _marker: core::marker::PhantomData,
        }
    }

    #[track_caller]
    pub fn start_frame(&mut self) -> usize {
        self.list.snapshot()
    }

    pub fn push_root(&mut self, interop_root: InteropRoot) -> Result<(), SystemError> {
        self.list.push(interop_root, ());

        Ok(())
    }
```
