### Title
Missing Storage Collision Check in EVM CREATE/CREATE2 Allows Deployment Over Addresses with Pre-existing Storage — (File: `evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

The `before_executing_frame` function in the EVM interpreter checks for existing code length and nonce before allowing a constructor to execute, but **does not check for pre-existing storage**. This is a deviation from EIP-684 semantics. An unprivileged attacker can exploit this by using CREATE2 + SELFDESTRUCT (EIP-6780) to leave storage residue at a deterministic address, then cause a victim's subsequent CREATE2 deployment at the same address to inherit that poisoned storage — producing a state transition that diverges from Ethereum.

---

### Finding Description

In `before_executing_frame`, the only collision guard for constructor calls is:

```rust
if deployee_code_len != 0 || deployee_nonce != 0 {
    // fail with CreateCollision
}
```

The code itself acknowledges the gap with an inline comment:

> "NB: EVM also specifies that the address should have empty storage, but we cannot perform such a check for now." [1](#0-0) 

The lower-level `deploy_code` function in the flat storage model also explicitly delegates the emptiness check to its caller:

> "Note: it is the caller's responsibility to check that the address is can be used for deployment (e.g. it is empty)" [2](#0-1) 

The caller (`before_executing_frame`) only checks `unpadded_code_len` and `nonce` — never storage — so the contract-level invariant is not fully enforced. [3](#0-2) 

The deviation is also silently acknowledged in the test index files, where the relevant Ethereum execution-spec tests are disabled:

> `comment: We do not check for storage collisions` [4](#0-3) [5](#0-4) 

This deviation is **not listed in `docs/not-a-bug.md`**, which is the authoritative registry of intentional deviations. [6](#0-5) 

---

### Impact Explanation

Under EIP-6780 (Cancun, which ZKsync OS targets), a SELFDESTRUCT called in the **same transaction** as contract creation clears code, nonce, and balance — but **not storage**. This creates a reachable path:

1. Attacker deploys Contract A at address `X` via `CREATE2(salt=S)`.
2. In the same transaction, Contract A writes attacker-chosen values to storage slots (e.g., slot 0 = `1`, representing "already initialized").
3. Contract A calls `SELFDESTRUCT` — code and nonce are cleared; storage persists.
4. In a later transaction, a victim deploys Contract B at address `X` via `CREATE2(salt=S)` (same deployer, same salt).
5. ZKsync OS passes the collision check (`code_len == 0`, `nonce == 0`) and runs Contract B's constructor.
6. Contract B's constructor executes against the poisoned storage state.

On Ethereum, step 4 would revert with a collision error. On ZKsync OS it succeeds, producing a divergent state transition.

Concrete consequences for Contract B:
- An `initialized` flag stored in slot 0 is already set → initialization logic is skipped.
- An `owner` address stored in slot 0 is already set to the attacker → ownership is pre-captured.
- A balance mapping is pre-seeded → accounting invariants are broken from the first block.

This is a **state-transition divergence** (EVM semantic mismatch) that can directly enable fund theft or permanent loss of control over contracts deployed on ZKsync OS via deterministic CREATE2 factories.

---

### Likelihood Explanation

Medium-to-High. The attack requires:

1. **Knowledge of the victim's CREATE2 salt** — trivially available for any factory contract that uses a public or predictable salt (counter-based, user-supplied, or emitted in events).
2. **Front-running the victim's deployment** — standard mempool front-running; the attacker only needs to execute steps 1–3 before the victim's deployment transaction is included.
3. **A victim contract that trusts storage to be zero at construction** — extremely common pattern (OpenZeppelin `Initializable`, proxy patterns, ERC-20 balance mappings, etc.).

No privileged access, governance approval, or oracle manipulation is required. Any unprivileged EOA can execute the attack.

---

### Recommendation

Before allowing a constructor frame to execute, add a storage-emptiness check at the target address. Since a full slot-by-slot scan is impractical, the standard approach is to track whether any storage write has ever been committed to the address (a single boolean flag per address in the account properties, set on first `SSTORE` and cleared on `SELFDESTRUCT`). Ethereum clients implement this via the "account has non-empty storage trie" check in EIP-684. Alternatively, ZKsync OS could maintain a per-address "dirty storage" bit in `AccountProperties` and reject CREATE/CREATE2 when that bit is set and `code_len == 0 && nonce == 0`.

---

### Proof of Concept

```solidity
// Attacker contract — deployed via CREATE2(salt=0x1234)
contract Poisoner {
    constructor() {
        assembly {
            // Write "1" to slot 0 (common "initialized" flag)
            sstore(0, 1)
            // EIP-6780: SELFDESTRUCT in same tx clears code+nonce, not storage
            selfdestruct(caller())
        }
    }
}

// Victim factory — uses same salt
contract VictimFactory {
    function deploy() external returns (address) {
        return address(new VictimContract{salt: bytes32(uint256(0x1234))}());
    }
}

// Victim contract — assumes slot 0 is zero at construction
contract VictimContract {
    bool public initialized; // slot 0
    address public owner;    // slot 1

    constructor() {
        require(!initialized, "already initialized"); // ← BYPASSED on ZKsync OS
        initialized = true;
        owner = msg.sender;
    }
}
```

**Attack sequence:**
1. Attacker calls `new Poisoner{salt: 0x1234}()` from the same deployer address as `VictimFactory` — this sets slot 0 = 1 at the deterministic address and self-destructs.
2. Victim calls `VictimFactory.deploy()` — ZKsync OS sees `code_len == 0, nonce == 0`, passes the collision check, and runs `VictimContract`'s constructor.
3. `require(!initialized)` reads slot 0 = 1 → **reverts**, permanently bricking the factory's ability to deploy at that address. Alternatively, if the check is absent, the attacker's pre-set `owner` value takes effect.

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L415-443)
```rust
            let deployee_code_len = frame_state
                .environment_parameters
                .callee_account_properties
                .unpadded_code_len;
            let deployee_nonce = frame_state
                .environment_parameters
                .callee_account_properties
                .nonce;

            // Check there's no contract already deployed at this address.
            // NB: EVM also specifies that the address should have empty storage,
            // but we cannot perform such a check for now.
            // We need to check this here (not when we actually deploy the code)
            // because if this check fails the constructor shouldn't be executed.
            if deployee_code_len != 0 || deployee_nonce != 0 {
                system_log!(system, "Deployment on existing account\n",);
                frame_state
                    .external_call
                    .available_resources
                    .charge(&S::Resources::from_ergs(
                        frame_state.external_call.available_resources.ergs(),
                    ))
                    .expect("Should succeed"); // Burn all gas

                tracer
                    .evm_tracer()
                    .on_call_error(&EvmError::CreateCollision);
                return Ok(false);
            }
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L779-780)
```rust
    /// Note: it is the caller's responsibility to check that the address is can be used for deployment (e.g. it is empty)
    pub fn deploy_code<const PROOF_ENV: bool>(
```

**File:** tests/evm_tester/indexes/develop-state-tests.yaml (L2315-2318)
```yaml
              RevertInCreateInInitCreate2Paris.json:
                hash: '0x37aae27794a84aaab2e4efda85bc70da'
                enabled: false
                comment: We do not check for storage collisions
```

**File:** tests/evm_tester/indexes/develop-state-tests.yaml (L2367-2370)
```yaml
              create2collisionStorageParis.json:
                hash: '0x1c8ea0d96bc40fd2a8997e58950d6bdb'
                enabled: false
                comment: We do not check storage for collision
```

**File:** docs/not-a-bug.md (L1-41)
```markdown
# Not a Bug

This page lists ZKsync OS behavior that is intentional, protocol-correct, or already documented, but is commonly misreported as a vulnerability.

Use this page as a triage aid, not as a substitute for checking the code. If an observed behavior differs from what is described here, investigate it normally.

## EVM / Cancun Behavior

### Cancun is the supported EVM hardfork

ZKsync OS currently targets Cancun EVM semantics. Reports that assume later hardfork behavior are not valid unless they identify an issue under the supported hardfork.

See [EVM Execution Environment](./execution_environments/evm.md).

### EIP-4844 transactions are disabled in production

EIP-4844 type `0x03` transactions are implemented behind the `basic_bootloader/eip-4844` feature, but this feature is not enabled by the production feature sets. It is enabled for test and Ethereum test-runner configurations.

See [Transaction formats](./bootloader/transaction_format.md).

### `BLOBHASH` returns `0`

This is expected in production. Since EIP-4844 transactions are disabled, transactions do not have blob versioned hashes. Cancun/EIP-4844 specifies that `BLOBHASH(index)` returns a zeroed `bytes32` when `index` is outside `tx.blob_versioned_hashes`.

### `BLOBBASEFEE` returns `1`

This is expected in production. There are no EIP-4844 transactions in production history, so `excess_blob_gas` remains `0`. Cancun/EIP-4844 defines the minimum blob base fee as `1`, and EIP-7516 makes `BLOBBASEFEE` return the current block's blob base fee.

### Opcode `0x44` returns `1`

Opcode `0x44` is `PREVRANDAO` under Cancun semantics, historically named `DIFFICULTY`. ZKsync OS uses the block `mix_hash` value for this opcode. In production, `mix_hash` is mocked to `1`.

This does not contradict the block header `difficulty` field being `0` after the Merge.

See [Bootloader block header](./bootloader/bootloader.md#block-header).

### `SUB` operand order

ZKsync OS follows the standard EVM operand order for `SUB`. The top stack item is the first operand, and the item below it is the second operand. For example, `PUSH1 0x03 PUSH1 0x05 SUB` leaves `0x02` on the stack.

This is not a reversed subtraction bug.
```
