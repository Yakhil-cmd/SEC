### Title
Unguarded `new` Initialization Allows Frontrunning to Seize Engine Ownership — (File: engine/src/contract_methods/admin.rs)

### Summary
The `new` function that initializes the Aurora Engine state performs no caller-identity check. Any NEAR account can call it before the legitimate deployer does. If the deployer submits the contract deployment and the `new` call as two separate transactions (rather than a single atomic batch), an attacker can observe the deployment and frontrun the initialization, installing themselves as the engine owner with attacker-chosen parameters.

### Finding Description
`new` in `engine/src/contract_methods/admin.rs` guards against double-initialization with a single existence check:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input)...;
    ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
```

`NewCallArgs` carries `owner_id`, `chain_id`, and `upgrade_delay_blocks`. There is no check that `env.predecessor_account_id()` equals `env.current_account_id()` or any other privileged identity. The function is a public NEAR contract method callable by any account on the network.

The attack window opens whenever the contract binary is deployed in one transaction and `new` is called in a subsequent, separate transaction. An attacker who observes the deployment transaction can race to submit their own `new` call with:
- `owner_id` = attacker's account
- `upgrade_delay_blocks` = 0 (immediate upgrades)

If the attacker's transaction lands first, the legitimate deployer's `new` call reverts with `ERR_ALREADY_INITIALIZED`, and the attacker holds full ownership of the engine.

This is the direct structural analog to M-17: a one-time initialization guarded only by an existence check, with no restriction on who may be the first caller, allowing a racing party to pre-populate the state and permanently deny the legitimate initializer.

### Impact Explanation
As engine owner the attacker can:
- Immediately upgrade the contract (with `upgrade_delay_blocks = 0`) to a malicious WASM binary
- Drain all ETH and ERC-20 tokens held by the ETH connector
- Pause the engine to freeze all in-flight user funds

Impact: **Critical — direct theft of all user funds at-rest and in-motion.**

### Likelihood Explanation
The attack requires the deployer to split deployment and initialization across two transactions rather than a single NEAR batch. This is a realistic operational mistake (e.g., a deployment script that issues `DeployContract` and `new` as separate RPC calls, or a manual deployment procedure). NEAR does not prevent a third party from submitting a transaction targeting the same contract method between two such calls. The attacker needs no privileged access — only the ability to submit a NEAR transaction, which any account can do.

Likelihood: **Low-to-medium** — the window exists only during deployment, but the consequence of the mistake is total loss of engine control.

### Recommendation
Add a predecessor-identity guard at the top of `new`:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    // Only the contract account itself (i.e. the deploying batch) may initialize.
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_NOT_AUTHORIZED".into());
    }
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    ...
}
```

Alternatively, enforce that deployment and initialization always occur in a single atomic NEAR batch action, and document this as a hard requirement enforced by the deployment tooling.

### Proof of Concept

1. Deployer submits TX-A: `DeployContract` to `aurora` (no `new` call in the same batch).
2. Attacker observes TX-A on the NEAR network.
3. Attacker submits TX-B: calls `aurora.new` with `owner_id = attacker.near`, `upgrade_delay_blocks = 0`.
4. TX-B is included before the deployer's TX-C (`aurora.new` with legitimate args).
5. TX-C reverts: `ERR_ALREADY_INITIALIZED`.
6. Attacker, now owner, calls `aurora.stage_upgrade` + `aurora.deploy_upgrade` with a malicious WASM that transfers all connector-held ETH to the attacker.

Relevant code: [1](#0-0) 

The `state::get_state` existence check is the sole gate: [2](#0-1) 

`EngineState` written on first call includes the attacker-supplied `owner_id`: [3](#0-2)

### Citations

**File:** engine/src/contract_methods/admin.rs (L55-88)
```rust
#[named]
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }

    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

    let initial_hashchain = args.initial_hashchain();
    let state: EngineState = args.into();

    if let Some(block_hashchain) = initial_hashchain {
        let block_height = env.block_height();
        let mut hashchain = Hashchain::new(
            state.chain_id,
            env.current_account_id(),
            block_height,
            block_hashchain,
        );

        hashchain.add_block_tx(
            block_height,
            function_name!(),
            &input,
            &[],
            &Bloom::default(),
        )?;
        crate::hashchain::save_hashchain(&mut io, &hashchain)?;
    }

    state::set_state(&mut io, &state)?;
    Ok(())
}
```
