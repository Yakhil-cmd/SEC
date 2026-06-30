### Title
Non-Atomic Update of Silo Parameters `fixed_gas` and `erc20_fallback_address` Enables Inconsistent State Leading to ERC-20 Token Misdirection - (File: `engine/src/contract_methods/silo/mod.rs`)

---

### Summary

The Aurora Engine exposes `set_fixed_gas` and `set_erc20_fallback_address` as independent admin-callable contract methods alongside the combined `set_silo_params`. Because `get_erc20_fallback_address` is consumed **directly** in `receive_erc20_tokens` independently of `fixed_gas`, an admin who disables fixed gas via `set_fixed_gas(None)` (instead of `set_silo_params(None)`) leaves the fallback-routing logic silently active. ERC-20 tokens sent to non-whitelisted addresses are then redirected to the fallback address even though `get_silo_params` returns `None` and the admin believes silo mode is fully disabled.

---

### Finding Description

**Two independent storage slots, two independent setters, one combined getter that is never the only consumer.**

`fixed_gas` and `erc20_fallback_address` are stored under separate keys: [1](#0-0) 

They can be written atomically via `set_silo_params`, which clears or sets both together: [2](#0-1) 

`get_silo_params` treats them as a pair and returns `None` if either slot is absent: [3](#0-2) 

However, the contract also exposes two **independent** admin entry-points: [4](#0-3) [5](#0-4) 

Critically, the two parameters are **consumed independently** in separate hot paths:

1. `fixed_gas` is read directly in `submit_with_alt_modexp` (transaction execution): [6](#0-5) 

2. `erc20_fallback_address` is read directly in `receive_erc20_tokens` (NEP-141 bridge inbound): [7](#0-6) 

Neither hot path calls `get_silo_params`; each reads its own slot in isolation. This means the two parameters are **not logically coupled at the consumption site**, only at the combined getter.

**Inconsistent state reachable via legitimate admin calls:**

| Action | `fixed_gas` slot | `erc20_fallback_address` slot | `get_silo_params` | Fallback routing active? |
|---|---|---|---|---|
| `set_silo_params(Some(…))` | set | set | `Some(…)` | yes |
| `set_fixed_gas(None)` only | **cleared** | **still set** | `None` | **yes** ← bug |
| `set_silo_params(None)` | cleared | cleared | `None` | no |

An admin who calls `set_fixed_gas(None)` to "turn off" silo mode (a natural interpretation of the function's name) leaves `erc20_fallback_address` populated. The whitelist remains enforced independently, so every subsequent `ft_on_transfer` for a non-whitelisted recipient silently mints ERC-20 tokens to the fallback address instead of the intended recipient.

---

### Impact Explanation

Any user who bridges a NEP-141 token into Aurora while the whitelist is active and their EVM address is not whitelisted will have their tokens minted to the fallback address rather than their own address. The tokens are not destroyed, but they are inaccessible to the user until the admin manually redistributes them from the fallback address. This constitutes **temporary freezing of user funds** (High).

The `SiloParamsArgs` documentation itself warns that the fallback logic "works only if the fallback address is set by `set_silo_params`": [8](#0-7) 

This comment implicitly acknowledges the coupling, yet the independent setters are still exposed and do not enforce it.

---

### Likelihood Explanation

The owner of the Aurora Engine contract is a single privileged account. The existence of `set_fixed_gas` and `set_erc20_fallback_address` as named, documented, separately-callable methods makes it natural for an operator to use them individually when they want to adjust only one parameter. The inconsistency is not surfaced by any on-chain check or error. Likelihood is **Low-Medium**: it requires an admin operational mistake, but the mistake is easy to make given the API surface.

---

### Recommendation

1. **Remove or restrict** `set_fixed_gas` and `set_erc20_fallback_address` as standalone admin entry-points, or make them internal helpers only callable from `set_silo_params`.
2. If independent setters must be kept, add a consistency check: when `set_fixed_gas(None)` is called, also clear `erc20_fallback_address`; when `set_erc20_fallback_address(None)` is called, also clear `fixed_gas`.
3. Alternatively, gate the fallback-routing logic in `receive_erc20_tokens` on `get_silo_params` (the combined check) rather than `get_erc20_fallback_address` alone, so that a missing `fixed_gas` slot disables fallback routing as well.

---

### Proof of Concept

```
1. Owner calls set_silo_params(Some(SiloParamsArgs {
       fixed_gas: 1_000_000,
       erc20_fallback_address: fallback_addr,
   }))
   → Both slots written. Silo mode active.

2. Owner decides to disable fixed-gas charging only:
       set_fixed_gas(None)
   → fixed_gas slot cleared.
   → erc20_fallback_address slot STILL SET.

3. Owner queries get_silo_params() → returns None.
   Owner believes silo mode is fully disabled.

4. User calls ft_transfer_call on a NEP-141 contract,
   routing tokens to Aurora with msg = <user_evm_address>.

5. receive_erc20_tokens executes:
       if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)   // Some(fallback_addr)
           && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)             // user not whitelisted
       {
           recipient = fallback_address;   // ← user's tokens go to fallback_addr
       }

6. ERC-20 tokens are minted to fallback_addr, not to the user.
   User's funds are inaccessible until admin manually redistributes.
```

### Citations

**File:** engine/src/contract_methods/silo/mod.rs (L16-17)
```rust
const GAS_COST_KEY: &[u8] = b"GAS_COST_KEY";
const ERC20_FALLBACK_KEY: &[u8] = b"ERC20_FALLBACK_KEY";
```

**File:** engine/src/contract_methods/silo/mod.rs (L20-28)
```rust
pub fn get_silo_params<I: IO>(io: &I) -> Option<SiloParamsArgs> {
    let params = get_fixed_gas(io)
        .and_then(|cost| get_erc20_fallback_address(io).map(|address| (cost, address)));

    params.map(|(cost, address)| SiloParamsArgs {
        fixed_gas: cost,
        erc20_fallback_address: address,
    })
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L31-38)
```rust
pub fn set_silo_params<I: IO>(io: &mut I, args: Option<SiloParamsArgs>) {
    let (cost, address) = args.map_or((None, None), |params| {
        (Some(params.fixed_gas), Some(params.erc20_fallback_address))
    });

    set_fixed_gas(io, cost);
    set_erc20_fallback_address(io, address);
}
```

**File:** engine/src/lib.rs (L783-793)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_fixed_gas() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: FixedGasArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_fixed_gas(&mut io, args.fixed_gas);
    }
```

**File:** engine/src/lib.rs (L805-815)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_erc20_fallback_address() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Erc20FallbackAddressArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_erc20_fallback_address(&mut io, args.address);
    }
```

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/engine.rs (L1049-1049)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);
```

**File:** engine-types/src/parameters/silo.rs (L19-23)
```rust
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
```
