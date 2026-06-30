### Title
`withdraw()` Does Not Check `EXIT_TO_ETHEREUM` Precompile Pause Status, Allowing Bypass of Emergency Pause - (File: `engine/src/contract_methods/connector.rs`)

### Summary

The Aurora Engine exposes a NEAR-level `withdraw()` function that allows ETH to be bridged from Aurora back to Ethereum. The engine also has a `pause_precompiles` mechanism that can pause the `EXIT_TO_ETHEREUM` precompile to halt ETH outflows during emergencies. However, the NEAR-level `withdraw()` function only checks `require_running()` (global engine pause) and never checks whether the `EXIT_TO_ETHEREUM` precompile is paused. Any unprivileged NEAR account can call `withdraw()` directly, bypassing the precompile-level pause entirely.

---

### Finding Description

The `pause_precompiles` admin function stores a `PrecompileFlags` bitmask in contract storage. The two pausable flags are `EXIT_TO_NEAR` and `EXIT_TO_ETHEREUM`. [1](#0-0) 

When the EVM executes a call to the `ExitToEthereum` precompile address, the engine checks this bitmask and rejects the call if the flag is set. This is the intended guard against ETH leaving Aurora to Ethereum during an emergency.

However, there is a second, entirely separate code path: the NEAR-level `withdraw()` function in `engine/src/contract_methods/connector.rs`. This function is callable directly by any NEAR account (it is a public contract method exposed via `lib.rs`). It constructs an `EngineWithdrawCallArgs` and dispatches a promise to `engine_withdraw` on the ETH connector contract — the exact same downstream action that the `ExitToEthereum` precompile triggers. [2](#0-1) 

The only guard in this function is `require_running()`, which checks the global `is_paused` flag on the engine state. It does **not** read `PrecompileFlags` from storage and does **not** call `is_paused(PrecompileFlags::EXIT_TO_ETHEREUM)`. [3](#0-2) 

The `pause_precompiles` and `resume_precompiles` admin functions write and read the `PAUSE_FLAGS` storage key exclusively through `EnginePrecompilesPauser`. [4](#0-3) 

The `withdraw()` function never instantiates `EnginePrecompilesPauser` and never reads `PAUSE_FLAGS`, so the pause state is invisible to it.

---

### Impact Explanation

The `EXIT_TO_ETHEREUM` pause flag exists precisely to stop ETH from leaving Aurora during an emergency (e.g., a bridge exploit, a connector accounting bug, or a governance incident). If that flag is set but the NEAR-level `withdraw()` path remains open, the pause provides no real protection: any user holding Aurora ETH can call `withdraw()` directly on the engine contract, bypassing the EVM precompile entirely, and drain their (or stolen) ETH to an Ethereum address before the incident is resolved.

**Impact: High — bypass of emergency pause enabling fund outflows that should be blocked; in an active exploit scenario this escalates to Critical (direct theft of funds at rest).**

---

### Likelihood Explanation

The `withdraw()` NEAR method is a standard, documented, publicly callable entry point. No special privilege is required. Any NEAR account that holds Aurora ETH (or has obtained it through an exploit) can call it at any time. The only precondition is that the engine is not globally paused (`is_paused = false`), which is the normal operating state. An admin who pauses only the precompile (the more surgical action) while leaving the engine running would be fully bypassed.

---

### Recommendation

Add a precompile-pause check at the top of `withdraw()` in `engine/src/contract_methods/connector.rs`, mirroring the check that the EVM enforces for the `ExitToEthereum` precompile:

```rust
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;

    // Mirror the EVM-level precompile pause check
    let pauser = EnginePrecompilesPauser::from_io(io);
    if pauser.is_paused(PrecompileFlags::EXIT_TO_ETHEREUM) {
        return Err(b"ERR_PAUSED".into());
    }

    env.assert_one_yocto()?;
    // ... rest unchanged
}
``` [5](#0-4) 

---

### Proof of Concept

1. Admin calls `pause_precompiles` with `paused_mask` = `EXIT_TO_ETHEREUM` bit set. The `PAUSE_FLAGS` storage key is updated. The EVM-level `ExitToEthereum` precompile now rejects all calls. [4](#0-3) 

2. Attacker (any NEAR account holding Aurora ETH) calls the NEAR-level `withdraw` method on the engine contract with a `WithdrawCallArgs` specifying their Ethereum recipient address and amount.

3. `withdraw()` executes: `require_running()` passes (engine is not globally paused), no precompile pause check is performed, and a promise to `engine_withdraw` on the ETH connector is dispatched. [2](#0-1) 

4. The ETH connector processes the withdrawal and the attacker receives ETH on Ethereum, despite the `EXIT_TO_ETHEREUM` precompile being paused. The emergency pause is fully circumvented.

### Citations

**File:** engine/src/pausables.rs (L9-17)
```rust
bitflags! {
    /// Wraps unsigned integer where each bit identifies a different precompile.
    #[derive(BorshSerialize, BorshDeserialize, Default)]
    #[borsh(crate = "aurora_engine_types::borsh")]
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
}
```

**File:** engine/src/pausables.rs (L146-154)
```rust
impl<I: IO> PausedPrecompilesChecker for EnginePrecompilesPauser<I> {
    fn is_paused(&self, precompiles: PrecompileFlags) -> bool {
        self.read_flags_from_storage().contains(precompiles)
    }

    fn paused(&self) -> PrecompileFlags {
        self.read_flags_from_storage()
    }
}
```

**File:** engine/src/contract_methods/connector.rs (L43-59)
```rust
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;

    let args: WithdrawCallArgs = io.read_input_borsh()?;
    let args = borsh::to_vec(&EngineWithdrawCallArgs {
        sender_id: env.predecessor_account_id(),
        recipient_address: args.recipient_address,
        amount: args.amount,
    })
    .unwrap();

    return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)
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
