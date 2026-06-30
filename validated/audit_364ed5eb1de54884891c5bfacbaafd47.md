### Title
Incomplete `EngineAuthorizer` ACL Implementation Makes `pause_precompiles` Dependent on a Single Point of Failure — (File: `engine/src/engine.rs`)

---

### Summary

The `EngineAuthorizer` struct contains an `acl: BTreeSet<AccountId>` field explicitly designed to hold multiple accounts authorized to pause precompiles. However, `get_authorizer` ignores this field entirely and hardcodes only the owner account via a self-acknowledged temporary workaround. No contract method exists to populate or persist the ACL. As a result, the intended multi-account authorization for `pause_precompiles` is non-functional: only the owner can ever pause the `EXIT_TO_NEAR` and `EXIT_TO_ETHEREUM` precompiles, leaving the bridge's emergency circuit-breaker with a single point of failure.

---

### Finding Description

In `engine/src/engine.rs`, `get_authorizer` is defined as:

```rust
pub fn get_authorizer<I: IO + Copy>(io: &I) -> EngineAuthorizer {
    // TODO: a temporary use the owner account only until the engine adapts std with near-plugins
    state::get_state(io)
        .map(|state| EngineAuthorizer::from_accounts(once(state.owner_id)))
        .unwrap_or_default()
}
```

The function:
1. Carries a `TODO` comment explicitly acknowledging it is a temporary workaround.
2. Constructs `EngineAuthorizer` with only `state.owner_id` — ignoring the `acl` field entirely.
3. Never reads from or writes to any storage key for the ACL.

The `EngineAuthorizer` struct in `engine/src/pausables.rs` is:

```rust
pub struct EngineAuthorizer {
    /// List of AccountIds with the permission to pause precompiles.
    pub acl: BTreeSet<AccountId>,
}
```

The `acl` field is designed to hold multiple authorized accounts, but:
- There is **no contract-callable method** to add accounts to the ACL.
- `EngineAuthorizer` is **never persisted to storage** — it is always reconstructed transiently.
- `get_authorizer` **ignores** the `acl` field and always returns an authorizer containing only the owner.

The `pause_precompiles` function in `engine/src/contract_methods/admin.rs` consumes this authorizer:

```rust
let authorizer: EngineAuthorizer = engine::get_authorizer(&io);
if !authorizer.is_authorized(&env.predecessor_account_id()) {
    return Err(b"ERR_UNAUTHORIZED".into());
}
```

Because `get_authorizer` always returns an authorizer containing only the owner, `pause_precompiles` is permanently restricted to the owner account. The `acl` field — and the entire multi-account delegation design — is dead code with no reachable write path.

This is structurally identical to the `dPrimeGuardian` bug: a security-critical data structure (`pipeAddresses` / `acl`) exists and is read from, but no setter exists to populate it, making the intended security mechanism non-functional.

---

### Impact Explanation

The `EXIT_TO_NEAR` and `EXIT_TO_ETHEREUM` precompiles are the bridge exit paths for ETH leaving Aurora. `pause_precompiles` is the designated emergency circuit-breaker for these paths. The design intent — evidenced by the `acl` field and the `Authorizer` trait — is that multiple trusted accounts can trigger a pause for rapid incident response.

Because the ACL cannot be populated, only the owner can pause the precompiles. If an exploit of either bridge precompile is in progress and the owner account is slow to respond (key rotation, multisig latency, operational unavailability), the exploit window remains open. Funds in transit through the bridge precompiles can be drained or permanently frozen during that window.

**Impact: High — Temporary freezing of funds / theft of funds in motion through the bridge exit precompiles.**

---

### Likelihood Explanation

Exploitation requires two concurrent conditions: (1) an active vulnerability in `EXIT_TO_NEAR` or `EXIT_TO_ETHEREUM`, and (2) the owner being unable to respond in time. Neither condition is trivially satisfied, but the design intent of the ACL was precisely to mitigate the second condition. The absence of the ACL setter removes that mitigation entirely.

**Likelihood: Low.**

---

### Recommendation

1. Add a contract method (e.g., `set_authorizer` or `add_pause_authorizer`) restricted to the owner that writes a populated `EngineAuthorizer` to a dedicated storage key.
2. Update `get_authorizer` to read the ACL from storage (falling back to the owner if no ACL is stored, for backward compatibility).
3. Remove the `TODO` comment and the temporary workaround once the above is implemented.

---

### Proof of Concept

1. Deploy Aurora Engine with any owner account `O`.
2. Attempt to call `pause_precompiles` from any account `A ≠ O`.
3. The call returns `ERR_UNAUTHORIZED` — confirmed by the check at `admin.rs:231`.
4. Inspect all contract-callable methods: no method exists to write to `EngineAuthorizer.acl` or to any storage key for the authorizer.
5. Inspect `get_authorizer` at `engine.rs:1255–1260`: it always constructs `EngineAuthorizer::from_accounts(once(state.owner_id))`, ignoring `acl`.
6. Conclusion: the `acl` field is permanently empty for any account other than the owner; the multi-account pause delegation is non-functional.

**Relevant code locations:**

- `get_authorizer` hardcoding only the owner: [1](#0-0) 

- `EngineAuthorizer.acl` field (designed for multi-account ACL, never populated): [2](#0-1) 

- `pause_precompiles` reading the authorizer and gating on it: [3](#0-2) 

- `Authorizer::is_authorized` checking the `acl` set: [4](#0-3)

### Citations

**File:** engine/src/engine.rs (L1255-1260)
```rust
pub fn get_authorizer<I: IO + Copy>(io: &I) -> EngineAuthorizer {
    // TODO: a temporary use the owner account only until the engine adapts std with near-plugins
    state::get_state(io)
        .map(|state| EngineAuthorizer::from_accounts(once(state.owner_id)))
        .unwrap_or_default()
}
```

**File:** engine/src/pausables.rs (L86-100)
```rust
#[derive(BorshSerialize, BorshDeserialize, Debug, Default, Clone)]
#[borsh(crate = "aurora_engine_types::borsh")]
pub struct EngineAuthorizer {
    /// List of [`AccountId`]s with the permission to pause precompiles.
    pub acl: BTreeSet<AccountId>,
}

impl EngineAuthorizer {
    /// Creates new [`EngineAuthorizer`] and grants permission to pause precompiles for all given `accounts`.
    pub fn from_accounts(accounts: impl Iterator<Item = AccountId>) -> Self {
        Self {
            acl: accounts.collect(),
        }
    }
}
```

**File:** engine/src/pausables.rs (L140-144)
```rust
impl Authorizer for EngineAuthorizer {
    fn is_authorized(&self, account: &AccountId) -> bool {
        self.acl.contains(account)
    }
}
```

**File:** engine/src/contract_methods/admin.rs (L225-241)
```rust
#[named]
pub fn pause_precompiles<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        require_running(&state::get_state(&io)?)?;
        let authorizer: EngineAuthorizer = engine::get_authorizer(&io);

        if !authorizer.is_authorized(&env.predecessor_account_id()) {
            return Err(b"ERR_UNAUTHORIZED".into());
        }

        let args: PausePrecompilesCallArgs = io.read_input_borsh()?;
        let flags = PrecompileFlags::from_bits_truncate(args.paused_mask);
        let mut pauser = EnginePrecompilesPauser::from_io(io);
        pauser.pause_precompiles(flags);
        Ok(())
    })
}
```
