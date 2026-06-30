### Title
Unchecked Precompile Call Return Value After `_burn` Enables Permanent Token Loss - (`File: etc/eth-contracts/contracts/EvmErc20.sol`, `File: etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, both `withdrawToNear` and `withdrawToEthereum` first burn the caller's ERC-20 tokens via `_burn`, then invoke the exit precompile via inline assembly. The return value `res` of the assembly `call` is captured but never checked. If the precompile call fails for any reachable reason (e.g., the precompile is paused), the burn is not reverted and the user's tokens are permanently destroyed with no corresponding cross-chain release.

---

### Finding Description

`EvmErc20.sol::withdrawToNear` (lines 53–63) and `EvmErc20.sol::withdrawToEthereum` (lines 65–76) follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
```

The identical pattern appears in `EvmErc20V2.sol` (lines 53–64 and 66–77).

The precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` (`exit_to_near::ADDRESS`) and `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` (`exit_to_ethereum::ADDRESS`) can legitimately fail and return `0` from the EVM `call` opcode in multiple reachable scenarios:

1. **Precompile is paused**: The engine supports pausing exit precompiles via `pause_precompiles`. When paused, `Precompiles::execute` returns `PrecompileFailure::Fatal { exit_status: ExitFatal::Other("ERR_PAUSED") }`, which causes the inner `call` to return `0`.
2. **ERC-20 not registered**: If `get_nep141_from_erc20` fails (`ERR_TARGET_TOKEN_NOT_FOUND`), the precompile returns an error and the `call` returns `0`.
3. **Out of gas** passed to the inner call.

In all these cases, `_burn` has already executed and is not reverted because the assembly block does not check `res` and does not `revert`. The outer function returns successfully, leaving the user with burned tokens and no cross-chain transfer. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

A user calling `withdrawToNear` or `withdrawToEthereum` while the corresponding exit precompile is paused will have their ERC-20 tokens burned with no corresponding NEP-141 or ETH release on the destination chain. The tokens are irrecoverably destroyed. There is no refund path triggered from the EVM side because the transaction does not revert. [4](#0-3) [3](#0-2) 

---

### Likelihood Explanation

**Medium.** The `pause_precompiles` function is a supported operational feature callable by authorized accounts. During any maintenance window or security incident where exit precompiles are paused, any user who calls `withdrawToNear` or `withdrawToEthereum` on a deployed `EvmErc20`/`EvmErc20V2` token will permanently lose their tokens. The entry path requires no special privilege — any token holder can trigger it. The paused state is a documented, reachable protocol state. [5](#0-4) 

---

### Recommendation

Check the return value of the assembly `call` and revert if it is zero, so that `_burn` is rolled back on precompile failure:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        if iszero(res) { revert(0, 0) }
    }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. [1](#0-0) [6](#0-5) 

---

### Proof of Concept

1. Deploy `EvmErc20` (or `EvmErc20V2`) as a bridged token on Aurora.
2. An authorized account calls `pause_precompiles` with `paused_mask = 0b01` (EXIT_TO_NEAR) or `0b10` (EXIT_TO_ETHEREUM).
3. A token holder calls `withdrawToNear(recipient, amount)` on the `EvmErc20` contract.
4. `_burn(_msgSender(), amount)` executes — the user's balance is reduced by `amount`.
5. The assembly `call` to `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` returns `0` because the precompile is paused (`ERR_PAUSED`).
6. `res` is never checked; the function returns without reverting.
7. The user's ERC-20 tokens are permanently destroyed. No NEP-141 tokens are transferred on NEAR. No refund occurs. [1](#0-0) [3](#0-2) [7](#0-6)

### Citations

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L66-77)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/lib.rs (L140-144)
```rust
        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }
```

**File:** engine/src/pausables.rs (L13-16)
```rust
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
```

**File:** engine/src/pausables.rs (L163-167)
```rust
    fn pause_precompiles(&mut self, precompiles_to_pause: PrecompileFlags) {
        let mut pause_flags = self.read_flags_from_storage();
        pause_flags.insert(precompiles_to_pause);
        self.write_flags_into_storage(pause_flags);
    }
```

**File:** engine/src/contract_methods/admin.rs (L226-240)
```rust
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
```
