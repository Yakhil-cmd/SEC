### Title
Single-Step Ownership Transfer Permanently Bricks Aurora Engine Owner Role - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The `set_owner` function in Aurora Engine performs a single-step, immediately-effective ownership transfer with no confirmation or pending state. If the current owner calls `set_owner` with an incorrect `new_owner` address (e.g., a typo, a burned address, or a misconfigured multisig), the `owner_id` is overwritten atomically and the old owner loses all control with no recovery path inside the contract.

---

### Finding Description

`set_owner` in `engine/src/contract_methods/admin.rs` reads the new owner from calldata and writes it directly to persistent state in a single atomic step:

```rust
// engine/src/contract_methods/admin.rs, lines 103-121
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let args: SetOwnerArgs = io.read_input_borsh()?;
        if state.owner_id == args.new_owner {
            return Err(errors::ERR_SAME_OWNER.into());
        }

        state.owner_id = args.new_owner;   // <-- immediate, irreversible write
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

The only guard is `ERR_SAME_OWNER` (prevents setting the same owner), but there is no validation that the new address is reachable, no pending/acceptance step, and no rollback mechanism.

The `owner_id` field gates every critical administrative function:

| Function | Guard |
|---|---|
| `upgrade` | `require_owner_only` |
| `stage_upgrade` | `require_owner_only` |
| `pause_contract` | `require_owner_only` |
| `resume_contract` | `require_owner_only` |
| `set_key_manager` | `require_owner_only` |
| `attach_full_access_key` | `require_owner_only` |
| `resume_precompiles` | `require_owner_only` |
| `factory_update` | `require_owner_only` |

If `owner_id` is set to an unreachable address, all of these functions are permanently inaccessible.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

The most direct path to fund freeze:

1. The current owner legitimately pauses the contract for maintenance via `pause_contract` (owner-only).
2. The owner then calls `set_owner` with an incorrect `new_owner` (e.g., a typo in a multisig address).
3. `state.owner_id` is immediately overwritten. The old owner has no control. The new address cannot sign.
4. `resume_contract` requires `require_owner_only` — it can never be called.
5. The contract is permanently paused. All user funds (ETH, bridged ERC-20s) are permanently frozen: no `submit`, no `call`, no `withdraw`, no `ft_transfer`.

Even without a prior pause, the loss of the owner role means:
- `upgrade` is permanently inaccessible — no security patches can ever be applied.
- `attach_full_access_key` is owner-only — no NEAR-level full access key can be added via the contract to recover.
- `pause_contract` is inaccessible — no emergency stop is possible if a critical exploit is found.

---

### Likelihood Explanation

**Low.** This requires an operational error by the current owner (e.g., passing a wrong NEAR account ID when transferring to a new multisig or DAO). The probability of such an error is low but non-zero, especially during governance transitions. The consequence is irreversible.

---

### Recommendation

Implement a two-step ownership transfer pattern:

1. Add a `pending_owner: Option<AccountId>` field to `EngineState`.
2. `set_owner` writes only to `pending_owner` (does not change `owner_id`).
3. Add a new `accept_owner` function that requires `predecessor_account_id == pending_owner` and then atomically sets `owner_id = pending_owner` and clears `pending_owner`.

This ensures the new owner can demonstrably sign transactions before the old owner loses control.

---

### Proof of Concept

**Entry path (NEAR transaction):**

```
Caller: current owner account (e.g., "aurora-owner.near")
Method: set_owner
Args (Borsh): SetOwnerArgs { new_owner: "aurrora-owner.near" }  // typo: double 'r'
```

**State after call:**

```
state.owner_id = "aurrora-owner.near"  // does not exist
```

**Consequence — attempt to resume a paused contract:**

```
Caller: "aurora-owner.near"  (old, legitimate owner)
Method: resume_contract
Result: ERR_NOT_ALLOWED  (owner_id != predecessor)

Caller: "aurrora-owner.near"  (typo address, no keypair)
Method: resume_contract
Result: unreachable — no one holds this key
```

The contract is permanently paused. All funds are permanently frozen.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine/src/contract_methods/admin.rs (L103-121)
```rust
#[named]
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;

        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let args: SetOwnerArgs = io.read_input_borsh()?;
        if state.owner_id == args.new_owner {
            return Err(errors::ERR_SAME_OWNER.into());
        }

        state.owner_id = args.new_owner;
        state::set_state(&mut io, &state)?;

        Ok(())
    })
}
```

**File:** engine/src/contract_methods/admin.rs (L263-271)
```rust
pub fn resume_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_paused(&state)?;
        state.is_paused = false;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
```

**File:** engine/src/contract_methods/mod.rs (L79-87)
```rust
pub fn require_owner_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    if &state.owner_id != predecessor_account_id {
        return Err(errors::ERR_NOT_ALLOWED.into());
    }
    Ok(())
}
```

**File:** engine-types/src/parameters/engine.rs (L117-122)
```rust
/// Borsh-encoded parameters for the `set_owner` function.
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
#[cfg_attr(feature = "impl-serde", derive(Serialize, Deserialize))]
pub struct SetOwnerArgs {
    pub new_owner: AccountId,
}
```
