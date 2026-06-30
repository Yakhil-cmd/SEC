### Title
Unpermissioned `deploy_erc20_token` Allows Any Caller to Pre-Register Arbitrary NEP-141 Account IDs, Permanently Blocking Token Bridging - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `deploy_erc20_token` entry point in Aurora Engine's connector module has no access control beyond `require_running`. Any unprivileged NEAR account can call it with an arbitrary NEP-141 account ID. Because `register_token` enforces uniqueness on the NEP-141 side and permanently stores the mapping, an attacker can pre-register any NEP-141 token ID (e.g., `usdc.near`) before the legitimate bridge operator does. All subsequent legitimate calls for that token fail with `TokenAlreadyRegistered`, permanently blocking the bridging of that token and freezing any in-flight funds.

---

### Finding Description

`deploy_erc20_token` in `engine/src/contract_methods/connector.rs` is the NEAR-callable entry point for bridging a NEP-141 token to Aurora as an ERC-20. Its only guard is `require_running`: [1](#0-0) 

There is no `require_owner_only`, no `env.assert_private_call()`, and no whitelist check. Any NEAR account can supply any `DeployErc20TokenArgs::Legacy(nep141)` value.

Internally, `engine::deploy_erc20_token` deploys the ERC-20 bytecode and then calls `register_token`: [2](#0-1) 

`register_token` checks only whether the NEP-141 side is already occupied: [3](#0-2) 

Once a NEP-141 account ID is registered, the mapping is permanent — there is no admin function to remove or replace it. The `BijectionMap::insert` writes both directions unconditionally: [4](#0-3) 

The analog to the Curves M-03 pattern is exact:

| Curves | Aurora Engine |
|---|---|
| Auto-counter generates `"CURVES5"` | Bridge operator calls `deploy_erc20_token("usdc.near")` |
| Attacker pre-registers symbol `"CURVES5"` | Attacker pre-calls `deploy_erc20_token("usdc.near")` |
| Legitimate auto-name fails: `InvalidERC20Metadata` | Legitimate bridge call fails: `TokenAlreadyRegistered` |
| Default naming permanently broken | Token permanently un-bridgeable |

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Once an attacker pre-registers `usdc.near` (or any high-value NEP-141), the legitimate bridge can never deploy the correct ERC-20 for that token. Any user who subsequently sends USDC (or the targeted token) through the NEAR→Aurora bridge via `ft_on_transfer` will have their tokens received by the connector but no corresponding ERC-20 minted, because `receive_erc20_tokens` depends on the NEP-141→ERC-20 mapping being correct: [5](#0-4) 

Funds sent to the bridge for a pre-registered-but-illegitimate token are permanently unrecoverable on the Aurora side.

---

### Likelihood Explanation

**High.** The attack requires only a single NEAR transaction with a trivially small gas cost. The attacker needs no special privileges, no tokens, and no prior state. The set of high-value NEP-141 account IDs (`usdc.near`, `wrap.near`, `dai.near`, etc.) is publicly known and finite. A single attacker transaction per target token is sufficient to permanently block bridging for that token.

---

### Recommendation

Add access control to `deploy_erc20_token` so that only the contract owner or a designated bridge operator can invoke it. The `WithMetadata` path already contains a comment acknowledging this intent ("this transaction could be executed by the owner of the contract only") but does not enforce it: [6](#0-5) 

Apply `require_owner_only(env, &state)?` (already used elsewhere in the codebase) to both the `Legacy` and `WithMetadata` branches of `deploy_erc20_token`, consistent with how `deploy_erc20_token_callback` uses `env.assert_private_call()`: [7](#0-6) 

---

### Proof of Concept

```
// Attacker NEAR account sends this transaction BEFORE the legitimate bridge operator:
aurora.deploy_erc20_token(
    DeployErc20TokenArgs::Legacy("usdc.near")
)
// → Succeeds. An ERC-20 is deployed at some address A_attacker.
// → register_token("usdc.near", A_attacker) is stored permanently.

// Later, legitimate bridge operator attempts:
aurora.deploy_erc20_token(
    DeployErc20TokenArgs::Legacy("usdc.near")
)
// → engine::deploy_erc20_token deploys ERC-20 at address A_legit (different nonce).
// → register_token("usdc.near", A_legit) is called.
// → get_erc20_from_nep141("usdc.near") returns Ok(A_attacker).
// → Returns Err(RegisterTokenError::TokenAlreadyRegistered).
// → deploy_erc20_token fails. "usdc.near" can never be legitimately bridged.

// Any user who sends USDC via ft_transfer_call to the Aurora connector:
// → ft_on_transfer is called with predecessor = "usdc.near"
// → receive_erc20_tokens looks up "usdc.near" → finds A_attacker (wrong contract)
// → Minting goes to the attacker-controlled ERC-20, or the call fails.
// → User funds are frozen/lost.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L81-90)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```

**File:** engine/src/contract_methods/connector.rs (L112-130)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
```

**File:** engine/src/contract_methods/connector.rs (L148-150)
```rust
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
```

**File:** engine/src/contract_methods/connector.rs (L161-170)
```rust
#[named]
pub fn deploy_erc20_token_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Address, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        env.assert_private_call()?;

```

**File:** engine/src/engine.rs (L722-741)
```rust
    pub fn register_token(
        &mut self,
        erc20_token: Address,
        nep141_token: AccountId,
    ) -> Result<(), RegisterTokenError> {
        match get_erc20_from_nep141(&self.io, &nep141_token) {
            Err(GetErc20FromNep141Error::Nep141NotFound) => (),
            Err(GetErc20FromNep141Error::InvalidNep141AccountId) => {
                return Err(RegisterTokenError::InvalidNep141AccountId);
            }
            Err(GetErc20FromNep141Error::InvalidAddress) => {
                return Err(RegisterTokenError::InvalidAddress);
            }
            Ok(_) => return Err(RegisterTokenError::TokenAlreadyRegistered),
        }

        let erc20_token = ERC20Address(erc20_token);
        let nep141_token = NEP141Account(nep141_token);
        nep141_erc20_map(self.io).insert(&nep141_token, &erc20_token);
        Ok(())
```

**File:** engine/src/engine.rs (L1359-1373)
```rust
    let address = match engine.deploy_code_with_input(input, None, handler) {
        Ok(result) => match result.status {
            TransactionStatus::Succeed(ret) => {
                Address::new(H160(ret.as_slice().try_into().unwrap()))
            }
            other => return Err(DeployErc20Error::Failed(other)),
        },
        Err(e) => return Err(DeployErc20Error::Engine(e)),
    };

    sdk::log!("Deployed ERC-20 in Aurora at: {:#?}", address);
    engine
        .register_token(address, nep141)
        .map_err(DeployErc20Error::Register)?;

```

**File:** engine/src/map.rs (L29-35)
```rust
    pub fn insert(&mut self, left: &L, right: &R) {
        let key = self.left_key(left);
        self.io.write_storage(&key, right.as_ref());

        let key = self.right_key(right);
        self.io.write_storage(&key, left.as_ref());
    }
```
