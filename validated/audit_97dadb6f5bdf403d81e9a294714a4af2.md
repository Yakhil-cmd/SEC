### Title
No User-Accessible Force-Exit Path When Exit Precompiles Are Paused — Temporary Freezing of ERC-20 Token Holders' Funds - (File: `engine/src/contract_methods/admin.rs`)

### Summary

When the Aurora Engine owner pauses both `ExitToNear` and `ExitToEthereum` precompiles via `pause_precompiles`, ERC-20 token holders on Aurora have no alternative exit path and no user-accessible bypass. Their bridged ERC-20 tokens are frozen until the owner calls `resume_precompiles`. This is the direct structural analog to the Gearbox report: an administrative "block" on a token prevents users from closing/exiting their position, and they must wait for a privileged party to unblock it.

### Finding Description

Aurora Engine exposes two exit precompiles as the **only** EVM-level mechanisms for users to bridge assets out of Aurora:

- `ExitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
- `ExitToEthereum` at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`

The `pause_precompiles` function in `engine/src/contract_methods/admin.rs` allows the owner to pause either or both of these precompiles by setting bits in a `PrecompileFlags` bitmask (`EXIT_TO_NEAR = 0b01`, `EXIT_TO_ETHEREUM = 0b10`). [1](#0-0) 

When a precompile address is in the `paused_precompiles` set, the `Precompiles::execute` dispatch in `engine-precompiles/src/lib.rs` returns a `PrecompileFailure::Fatal { exit_status: ExitFatal::Other("ERR_PAUSED") }` for every call to that address, with no bypass: [2](#0-1) 

The `PrecompileFlags` bitmask covers exactly these two precompiles and nothing else: [3](#0-2) 

The `get_authorizer` function currently grants pause permission only to the owner account: [4](#0-3) 

`resume_precompiles` is also owner-only via `require_owner_only`: [5](#0-4) 

**Critical design gap:** For ERC-20 tokens, the `ExitToNear` and `ExitToEthereum` precompiles are the **only** exit paths. The NEAR-level `withdraw` function in `engine/src/contract_methods/connector.rs` handles only the ETH base token (it calls `engine_withdraw` on the eth-connector), not ERC-20 tokens: [6](#0-5) 

There is no `force` flag, no alternative exit function, and no time-bounded pause. When both precompiles are paused simultaneously (mask `0b11`), ERC-20 token holders are completely frozen with no self-service recourse.

### Impact Explanation

**High — Temporary freezing of funds.**

Any user holding bridged ERC-20 tokens on Aurora (NEP-141 tokens mirrored as ERC-20s) loses the ability to exit those tokens for the entire duration that both exit precompiles are paused. There is no user-accessible bypass. The freeze persists until the owner calls `resume_precompiles`. Unlike ETH (base token) holders who retain a NEAR-level `withdraw` path, ERC-20 token holders have zero alternative exit mechanism.

### Likelihood Explanation

The `pause_precompiles` function is an intentional, production-deployed administrative feature designed for emergency security response. The owner can legitimately pause both precompiles simultaneously with a single call using mask `0b11`. There is no on-chain time limit enforcing how long the pause can last. Any security incident that triggers a precompile pause will simultaneously freeze all ERC-20 token holders' exit rights with no user recourse. The likelihood is realistic given that the pause mechanism is explicitly designed to be used.

### Recommendation

Add a user-accessible force-exit path analogous to the Gearbox recommendation. Specifically:

1. Introduce a time-bounded pause: after a configurable maximum pause duration, allow users to call a force-exit function that bypasses the pause flag.
2. Or, provide a separate `force_exit_erc20` NEAR-level method (callable directly, not through the EVM precompile dispatch) that allows users to withdraw their ERC-20 token balances even when the precompiles are paused, similar to how the NEAR-level `withdraw` provides a base-token exit path independent of the EVM precompile layer.
3. At minimum, document and enforce a maximum pause duration in the contract state.

### Proof of Concept

1. Owner calls `pause_precompiles` with `paused_mask = 0b11` (both `EXIT_TO_NEAR` and `EXIT_TO_ETHEREUM`).
2. User holding ERC-20 tokens on Aurora attempts to exit via `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`).
3. `Precompiles::execute` checks `self.is_paused(&address)` → `true` → returns `PrecompileFailure::Fatal { exit_status: ExitFatal::Other("ERR_PAUSED") }`. [2](#0-1) 

4. User attempts `ExitToEthereum` precompile (`0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) → same `ERR_PAUSED` result.
5. User attempts NEAR-level `withdraw` → this only handles ETH base token, not ERC-20 tokens; ERC-20 exit is impossible.
6. User's ERC-20 tokens remain frozen on Aurora until the owner calls `resume_precompiles`. This is confirmed by the existing test: [7](#0-6)

### Citations

**File:** engine/src/contract_methods/admin.rs (L208-223)
```rust
#[named]
pub fn resume_precompiles<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let predecessor_account_id = env.predecessor_account_id();

        require_owner_only(&state, &predecessor_account_id)?;

        let args: PausePrecompilesCallArgs = io.read_input_borsh()?;
        let flags = PrecompileFlags::from_bits_truncate(args.paused_mask);
        let mut pauser = EnginePrecompilesPauser::from_io(io);
        pauser.resume_precompiles(flags);
        Ok(())
    })
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

**File:** engine-precompiles/src/lib.rs (L140-144)
```rust
        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }
```

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

**File:** engine/src/engine.rs (L1255-1260)
```rust
pub fn get_authorizer<I: IO + Copy>(io: &I) -> EngineAuthorizer {
    // TODO: a temporary use the owner account only until the engine adapts std with near-plugins
    state::get_state(io)
        .map(|state| EngineAuthorizer::from_accounts(once(state.owner_id)))
        .unwrap_or_default()
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

**File:** engine-tests/src/tests/pausable_precompiles.rs (L36-55)
```rust
#[test]
fn test_executing_paused_precompile_throws_error() {
    let (mut runner, mut signer, _, tester) = setup_test();

    let call_args = PausePrecompilesCallArgs {
        paused_mask: EXIT_TO_ETHEREUM_FLAG,
    };
    let input = borsh::to_vec(&call_args).unwrap();

    let _res = runner.call(PAUSE_PRECOMPILES, CALLED_ACCOUNT_ID, input);
    let is_to_near = false;
    let error = tester
        .withdraw(&mut runner, &mut signer, is_to_near)
        .unwrap_err();

    assert!(matches!(
        error.kind,
        EngineErrorKind::EvmFatal(aurora_evm::ExitFatal::Other(e)) if e == "ERR_PAUSED"
    ));
}
```
