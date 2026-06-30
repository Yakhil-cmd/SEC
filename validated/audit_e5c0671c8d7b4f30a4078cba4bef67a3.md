### Title
Silo Mode Whitelist Bypass via `call` Entrypoint Allows Non-Whitelisted NEAR Accounts to Execute Arbitrary EVM Transactions and Steal Tokens - (File: `engine/src/contract_methods/evm_transactions.rs`)

---

### Summary

The `call` entrypoint in Aurora Engine's silo mode does not perform any whitelist access check, unlike the `submit` entrypoint which enforces `assert_access`. Any NEAR account — regardless of whitelist status — can invoke `call` to execute arbitrary EVM transactions. This is a direct structural analog to the reported `StakedToken` bug: the restriction is checked on one execution path (`submit`) but silently absent on another (`call`), allowing a non-whitelisted actor to act as the EVM `msg.sender` and drain tokens from whitelisted addresses that have granted ERC-20 allowances.

---

### Finding Description

In silo mode, Aurora Engine enforces a two-dimensional whitelist:
- `WhitelistKind::Account` — NEAR account IDs allowed to submit transactions
- `WhitelistKind::Address` — EVM addresses allowed to submit transactions

The `submit` and `submit_with_args` entrypoints enforce this via `assert_access`:

```rust
// engine/src/engine.rs:1051-1052
// Check if the sender has rights to submit transactions or deploy code.
assert_access(&io, env, &transaction)?;
```

`assert_access` calls `silo::is_allow_submit` which checks both the NEAR predecessor account and the EVM transaction signer address against their respective whitelists.

However, the `call` entrypoint performs **no whitelist check whatsoever**:

```rust
// engine/src/contract_methods/evm_transactions.rs:46-71
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;          // only checks: is contract paused?
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes).ok_or(errors::ERR_BORSH_DESERIALIZE)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id), // EVM sender = hash(NEAR caller)
            current_account_id, io, env,
        );
        let result = engine.call_with_args(args, handler)?;  // no assert_access
        ...
    })
}
```

The EVM `msg.sender` inside the `call` execution is `predecessor_address(predecessor_account_id)` — a deterministic EVM address derived from the NEAR caller's account ID. Any NEAR account can call this entrypoint and execute EVM transactions as their derived EVM address, with no whitelist enforcement.

**Exploit path for token theft:**

1. Alice (whitelisted EVM address) holds ERC-20 tokens on the silo and has previously called `approve(bob_evm_addr, amount)` via `submit` (which was allowed because Alice was whitelisted at the time).
2. Bob's NEAR account is NOT in the `Account` whitelist. Bob's EVM address (`predecessor_address(bob.near)`) is NOT in the `Address` whitelist.
3. Bob calls the `call` NEAR entrypoint with calldata encoding `transferFrom(alice_evm_addr, bob_evm_addr, amount)` targeting the ERC-20 contract.
4. The `call` entrypoint skips all whitelist checks, executes the EVM call with `msg.sender = bob_evm_addr`, and the ERC-20 `transferFrom` succeeds because the allowance exists.
5. Alice's tokens are transferred to Bob's EVM address. Bob can then bridge them out via `withdrawToNear`.

The `EvmErc20` contract's `withdrawToNear` burns `_msgSender()`'s tokens, so Bob can also exit his own balance directly via `call` → `withdrawToNear`.

---

### Impact Explanation

**Impact: High — Theft of tokens (ERC-20 balances) held by whitelisted addresses.**

Any non-whitelisted NEAR account can bypass the silo whitelist entirely by using `call` instead of `submit`. If any whitelisted address has granted an ERC-20 allowance to an address derived from a non-whitelisted NEAR account (e.g., via a prior interaction, a DeFi protocol approval, or a contract that auto-approves), the non-whitelisted account can drain those tokens. The silo whitelist — the primary access-control mechanism in silo mode — is rendered ineffective for the `call` execution path.

---

### Likelihood Explanation

**Likelihood: High.**

The `call` entrypoint is a standard, publicly documented Aurora Engine interface callable by any NEAR account. No special privileges, leaked keys, or governance capture are required. The only precondition for direct token theft is the existence of an ERC-20 allowance from a whitelisted address to the attacker's derived EVM address — a realistic condition in any silo that hosts DeFi protocols (which routinely involve `approve` flows). Even without an existing allowance, the whitelist bypass itself is unconditional and allows non-whitelisted accounts to interact with any EVM contract on the silo.

---

### Recommendation

Apply the same `assert_access` check used in `submit_with_alt_modexp` to the `call` entrypoint. Specifically, derive the EVM address from the NEAR predecessor account and validate it against the silo `Address` and `Account` whitelists before executing the EVM call:

```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let predecessor_account_id = env.predecessor_account_id();
        let predecessor_evm_address = predecessor_address(&predecessor_account_id);

        // Add whitelist check analogous to assert_access in submit:
        if !silo::is_allow_submit(&io, &predecessor_account_id, &predecessor_evm_address) {
            return Err(ContractError::msg(errors::ERR_NOT_ALLOWED));
        }
        // ... rest of call logic
    })
}
```

The same fix should be applied to `deploy_code`, which also lacks a whitelist check.

---

### Proof of Concept

```
Setup (silo mode, whitelists enabled):
  - Alice's NEAR account: alice.near  → EVM address: alice_evm (whitelisted)
  - Bob's NEAR account:   bob.near    → EVM address: bob_evm   (NOT whitelisted)
  - ERC-20 token: erc20_addr

Step 1 (Alice, whitelisted, via submit):
  Alice calls submit() with tx: erc20_addr.approve(bob_evm, 1000e18)
  → Passes assert_access (Alice is whitelisted)
  → bob_evm now has allowance of 1000e18 from alice_evm

Step 2 (Bob, NOT whitelisted, via call):
  Bob calls the NEAR `call` entrypoint with:
    CallArgs { contract: erc20_addr,
               input: abi.encode(transferFrom(alice_evm, bob_evm, 1000e18)) }
  → call() skips assert_access entirely
  → EVM executes with msg.sender = predecessor_address("bob.near") = bob_evm
  → transferFrom succeeds (allowance exists)
  → Alice loses 1000e18 tokens; Bob gains them

Step 3 (Bob exits):
  Bob calls `call` with withdrawToNear calldata on erc20_addr
  → Burns bob_evm's balance, triggers ExitToNear precompile
  → Bob receives NEP-141 tokens on NEAR
```

**Root cause lines:**
- Missing check: [1](#0-0) 
- Present check in `submit`: [2](#0-1) 
- `assert_access` implementation: [3](#0-2) 
- `is_allow_submit` checks both account and address whitelists: [4](#0-3) 
- ERC-20 `transferFrom` is inherited from OpenZeppelin ERC20 (standard allowance mechanism): [5](#0-4) 
- `withdrawToNear` burns `_msgSender()` tokens, usable by Bob after theft: [6](#0-5)

### Citations

**File:** engine/src/contract_methods/evm_transactions.rs (L46-71)
```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes).ok_or(errors::ERR_BORSH_DESERIALIZE)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();

        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id),
            current_account_id,
            io,
            env,
        );
        let result = engine.call_with_args(args, handler)?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);
        Ok(result)
    })
}
```

**File:** engine/src/engine.rs (L1051-1052)
```rust
    // Check if the sender has rights to submit transactions or deploy code.
    assert_access(&io, env, &transaction)?;
```

**File:** engine/src/engine.rs (L1756-1775)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L15-15)
```text
contract EvmErc20 is ERC20, AdminControlled, IExit {
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```
